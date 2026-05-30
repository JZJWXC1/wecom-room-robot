import logging
import shutil
import subprocess
from pathlib import Path


logger = logging.getLogger("room-robot")

WECOM_VIDEO_MAX_BYTES = 9 * 1024 * 1024


class VideoTranscodeError(RuntimeError):
    pass


def needs_wecom_video_transcode(path: Path, max_bytes: int = WECOM_VIDEO_MAX_BYTES) -> bool:
    try:
        return path.stat().st_size > max_bytes
    except OSError:
        return False


def prepare_wecom_video(
    source: Path,
    *,
    force: bool = False,
    max_bytes: int = WECOM_VIDEO_MAX_BYTES,
) -> Path:
    if not force and not needs_wecom_video_transcode(source, max_bytes=max_bytes):
        return source

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise VideoTranscodeError("未找到 ffmpeg，无法压缩企业微信视频")

    cache_dir = source.parent / ".wecom_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    output = cache_dir / f"{source.stem}.wecom.mp4"
    if (
        output.exists()
        and output.stat().st_mtime >= source.stat().st_mtime
        and output.stat().st_size <= max_bytes
    ):
        return output

    scale_filter = "scale=-2:720,fps=24"
    last_error = ""
    for crf in (28, 32, 36):
        tmp_output = output.with_suffix(f".crf{crf}.tmp.mp4")
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(source),
            "-vf",
            scale_filter,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "veryfast",
            "-crf",
            str(crf),
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-movflags",
            "+faststart",
            str(tmp_output),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
        if result.returncode != 0:
            last_error = result.stderr[-1000:]
            logger.warning("企业微信视频转码失败: %s", last_error)
            continue
        tmp_output.replace(output)
        if output.stat().st_size <= max_bytes:
            return output
        last_error = f"转码后仍超过限制: {output.stat().st_size} bytes"

    if output.exists():
        return output
    raise VideoTranscodeError(last_error or "企业微信视频转码失败")
