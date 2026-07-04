# -*- coding: utf-8 -*-
"""原视频等大文件的签名直链。

发送阶段用 build_signed_media_url 生成带 HMAC-SHA256 签名与过期时间的下载链接,
路由端用 verify_signed_media_request 校验后回源文件。设计约束:
- 签名与校验共用同一路径规范形(正斜杠、相对路径、无穿越段),防篡改防目录穿越;
- 任何校验失败一律抛 ValueError(fail-closed),路由端统一转 404,不泄漏差异信息;
- 本模块保持纯函数,不读 settings/不做 IO 之外的副作用,便于单测与复用。
"""
from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlencode

SIGNED_MEDIA_ROUTE = "/wecom/media/original"
DEFAULT_LINK_TTL_SECONDS = 48 * 3600
MIN_LINK_TTL_SECONDS = 60


def _canonical_rel_path(rel_path: str) -> str:
    text = str(rel_path or "").strip().replace("\\", "/")
    if not text or text.startswith("/") or text.startswith("~"):
        raise ValueError("rel_path must be a relative path")
    pure = PurePosixPath(text)
    if any(part in ("..", ".") for part in pure.parts):
        raise ValueError("rel_path must not contain traversal segments")
    return str(pure)


def sign_media_link(rel_path: str, expires_at: int, secret: str) -> str:
    if not str(secret or "").strip():
        raise ValueError("secret required")
    canonical = _canonical_rel_path(rel_path)
    message = f"{canonical}|{int(expires_at)}".encode("utf-8")
    return hmac.new(str(secret).encode("utf-8"), message, hashlib.sha256).hexdigest()


def build_signed_media_url(
    *,
    public_base_url: str,
    rel_path: str,
    secret: str,
    ttl_seconds: int = DEFAULT_LINK_TTL_SECONDS,
    now: float | None = None,
) -> str:
    base = str(public_base_url or "").rstrip("/")
    if not base:
        raise ValueError("public_base_url required")
    current = time.time() if now is None else float(now)
    expires_at = int(current + max(MIN_LINK_TTL_SECONDS, int(ttl_seconds)))
    canonical = _canonical_rel_path(rel_path)
    token = sign_media_link(canonical, expires_at, secret)
    query = urlencode(
        {"file": canonical, "expires": expires_at, "token": token},
        quote_via=quote,
    )
    return f"{base}{SIGNED_MEDIA_ROUTE}?{query}"


def verify_signed_media_request(
    *,
    rel_path: str,
    expires: int | str,
    token: str,
    secret: str,
    media_root: Path,
    now: float | None = None,
) -> Path:
    """校验签名/时效/路径边界,返回可回源的绝对路径;失败抛 ValueError。"""
    try:
        expires_at = int(expires)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid expires") from error
    current = time.time() if now is None else float(now)
    if current > expires_at:
        raise ValueError("link expired")
    canonical = _canonical_rel_path(rel_path)
    expected = sign_media_link(canonical, expires_at, secret)
    if not hmac.compare_digest(expected, str(token or "")):
        raise ValueError("signature mismatch")
    root = Path(media_root).resolve()
    target = (root / PurePosixPath(canonical)).resolve()
    if root != target and root not in target.parents:
        raise ValueError("path outside media root")
    if not target.is_file():
        raise ValueError("file not found")
    return target
