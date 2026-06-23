from __future__ import annotations

import re
import shutil
import subprocess
import os
from pathlib import Path
from typing import Any

from app.services.feishu import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, is_deleted_note_error
from app.services.region_inventory_constants import DRIVE_UPLOAD_SAFE_VIDEO_BYTES
from app.services.region_inventory_utils import safe_name


def should_transcode_mov_upload_fallback(suffix: str, exc: BaseException) -> bool:
    if suffix.casefold() not in VIDEO_EXTENSIONS:
        return False
    message = str(exc).casefold()
    return "params error" in message or "invalid params" in message


def probe_video_duration_seconds(ffmpeg: str, source: Path, *, timeout: int = 30) -> float:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(source)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    output = f"{result.stderr}\n{result.stdout}"
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    if not match:
        raise RuntimeError("无法读取视频时长，不能按目标大小压缩")
    hours, minutes, seconds = match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if duration <= 0:
        raise RuntimeError("视频时长无效，不能按目标大小压缩")
    return duration


def calculate_target_video_bitrate_kbps(
    *,
    duration_seconds: float,
    max_bytes: int = DRIVE_UPLOAD_SAFE_VIDEO_BYTES,
    audio_kbps: int = 96,
    reserve_bytes: int = 256 * 1024,
) -> int:
    usable_bytes = max(512 * 1024, max_bytes - reserve_bytes)
    total_kbps = int((usable_bytes * 8) / max(duration_seconds, 1.0) / 1000)
    return max(350, total_kbps - audio_kbps)


def transcode_video_to_mp4(
    source: Path,
    target: Path,
    *,
    timeout: int = 240,
    max_bytes: int = DRIVE_UPLOAD_SAFE_VIDEO_BYTES,
) -> Path:
    ffmpeg = resolve_ffmpeg_executable()
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg，无法将 mov 转成 mp4")
    duration = probe_video_duration_seconds(ffmpeg, source)
    target.parent.mkdir(parents=True, exist_ok=True)
    last_error = ""
    for scale in (1.0, 0.92, 0.84):
        target_bytes = int(max_bytes * scale)
        video_kbps = calculate_target_video_bitrate_kbps(
            duration_seconds=duration,
            max_bytes=target_bytes,
        )
        pass_log = target.with_suffix(f".pass{int(scale * 100)}")
        tmp_target = target.with_suffix(f".target{int(scale * 100)}.tmp.mp4")
        common = [
            ffmpeg,
            "-y",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-vf",
            "scale=w='min(1920,iw)':h='min(1920,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,fps=30",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-b:v",
            f"{video_kbps}k",
            "-maxrate",
            f"{video_kbps}k",
            "-bufsize",
            f"{video_kbps * 2}k",
            "-pix_fmt",
            "yuv420p",
        ]
        first_pass = [
            *common,
            "-an",
            "-pass",
            "1",
            "-passlogfile",
            str(pass_log),
            "-f",
            "null",
            os.devnull,
        ]
        second_pass = [
            *common,
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-pass",
            "2",
            "-passlogfile",
            str(pass_log),
            "-movflags",
            "+faststart",
            str(tmp_target),
        ]
        try:
            first_result = subprocess.run(first_pass, capture_output=True, text=True, timeout=timeout, check=False)
            result = subprocess.run(second_pass, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"mov 转 mp4 超过 {timeout} 秒") from exc
        finally:
            for path in target.parent.glob(f"{pass_log.name}*"):
                path.unlink(missing_ok=True)
        if first_result.returncode != 0:
            last_error = (first_result.stderr or "mov 转 mp4 第一遍失败")[-1000:]
            continue
        if result.returncode != 0:
            last_error = (result.stderr or "mov 转 mp4 失败")[-1000:]
            continue
        tmp_target.replace(target)
        if not target.is_file() or target.stat().st_size <= 0:
            last_error = "mov 转 mp4 后文件为空"
            continue
        if target.stat().st_size <= max_bytes:
            return target
        last_error = f"mov 转 mp4 后仍超过上传安全大小：{target.stat().st_size} bytes"
    if target.is_file() and target.stat().st_size > 0:
        return target
    raise RuntimeError(last_error or "mov 转 mp4 失败")


def resolve_ffmpeg_executable() -> str:
    ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if ffmpeg:
        return ffmpeg
    try:
        import imageio_ffmpeg
    except ImportError:
        return ""
    candidate = Path(imageio_ffmpeg.get_ffmpeg_exe())
    return str(candidate) if candidate.is_file() else ""

def extract_note_links(record: dict[str, Any]) -> list[str]:
    links: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("link", "url", "href"):
                link = str(value.get(key) or "")
                if "feishu.cn" in link or "larksuite.com" in link:
                    links.append(link)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str) and ("feishu.cn" in value or "larksuite.com" in value):
            links.append(value)

    walk(record.get("fields") or {})
    return list(dict.fromkeys(links))


def extract_docx_mentions(record: dict[str, Any]) -> list[dict[str, str]]:
    documents: dict[str, dict[str, str]] = {}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            mention_type = str(value.get("mentionType") or value.get("realMentionType") or "")
            token = str(value.get("token") or "")
            if mention_type == "Docx" and token:
                documents[token] = {
                    "token": token,
                    "title": str(value.get("text") or token),
                    "url": str(value.get("link") or value.get("url") or ""),
                }
                return
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(record.get("fields") or {})
    return list(documents.values())


def extract_docx_media_attachments(blocks: list[dict[str, Any]], title: str) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_media(media: dict[str, Any], default_name: str) -> None:
        token = str(
            media.get("file_token")
            or media.get("token")
            or media.get("fileKey")
            or media.get("media_id")
            or ""
        )
        if not token or token in seen:
            return
        name = str(media.get("name") or media.get("file_name") or "").strip()
        if not name:
            name = default_name + extension_from_media(media)
        suffix = Path(name).suffix.lower()
        if suffix and suffix not in IMAGE_EXTENSIONS and suffix not in VIDEO_EXTENSIONS:
            return
        seen.add(token)
        attachments.append({"name": name, "file_token": token})

    for index, block in enumerate(blocks, start=1):
        if not isinstance(block, dict):
            continue
        for key, default_suffix in (
            ("image", "图片"),
            ("video", "视频"),
            ("file", "文件"),
        ):
            media = block.get(key)
            if isinstance(media, dict):
                add_media(media, f"{title}-{default_suffix}{index:02d}")
        nested_attachments = []
        collect_docx_media_dicts(block, nested_attachments)
        for media in nested_attachments:
            add_media(media, f"{title}-素材{index:02d}")
    return attachments


def collect_docx_media_dicts(value: Any, media_items: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        if value.get("file_token") or value.get("fileKey") or value.get("media_id"):
            media_items.append(value)
            return
        for item in value.values():
            collect_docx_media_dicts(item, media_items)
    elif isinstance(value, list):
        for item in value:
            collect_docx_media_dicts(item, media_items)


def extension_from_media(media: dict[str, Any]) -> str:
    name = str(media.get("name") or media.get("file_name") or "")
    suffix = Path(name).suffix.lower()
    if suffix:
        return suffix
    mime_type = str(media.get("mime_type") or media.get("content_type") or "").lower()
    if "png" in mime_type:
        return ".png"
    if "webp" in mime_type:
        return ".webp"
    if "jpeg" in mime_type or "jpg" in mime_type or "image" in mime_type:
        return ".jpg"
    if "quicktime" in mime_type:
        return ".mov"
    if "video" in mime_type:
        return ".mp4"
    return ".jpg"


def brief_error(exc: Exception) -> str:
    if is_deleted_note_error(exc):
        return "飞书房源笔记已删除"
    text = str(exc)
    if "docx:document" in text or "Access denied" in text or "99991672" in text:
        return "飞书应用缺少 docx:document:readonly 文档只读权限"
    text = re.sub(r"https?://\\S+", "", text)
    return text[:300]
