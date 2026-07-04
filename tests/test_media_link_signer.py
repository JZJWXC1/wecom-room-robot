# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.media_link_signer import (
    SIGNED_MEDIA_ROUTE,
    build_signed_media_url,
    sign_media_link,
    verify_signed_media_request,
)

SECRET = "unit-test-secret"


def _make_media_file(tmp_path: Path) -> Path:
    root = tmp_path / "room_database"
    (root / "videos").mkdir(parents=True)
    target = root / "videos" / "石桥铭苑6-1102.mp4"
    target.write_bytes(b"fake-video-bytes")
    return root


def test_signed_url_roundtrip_verifies_and_resolves_file(tmp_path: Path) -> None:
    root = _make_media_file(tmp_path)
    url = build_signed_media_url(
        public_base_url="https://ynzyqbot.cn/",
        rel_path="videos/石桥铭苑6-1102.mp4",
        secret=SECRET,
        ttl_seconds=3600,
        now=1_000_000,
    )
    assert url.startswith(f"https://ynzyqbot.cn{SIGNED_MEDIA_ROUTE}?")
    assert "expires=1003600" in url

    resolved = verify_signed_media_request(
        rel_path="videos/石桥铭苑6-1102.mp4",
        expires=1_003_600,
        token=sign_media_link("videos/石桥铭苑6-1102.mp4", 1_003_600, SECRET),
        secret=SECRET,
        media_root=root,
        now=1_000_100,
    )
    assert resolved == (root / "videos" / "石桥铭苑6-1102.mp4").resolve()


def test_expired_link_is_rejected(tmp_path: Path) -> None:
    root = _make_media_file(tmp_path)
    token = sign_media_link("videos/石桥铭苑6-1102.mp4", 1_003_600, SECRET)
    with pytest.raises(ValueError, match="expired"):
        verify_signed_media_request(
            rel_path="videos/石桥铭苑6-1102.mp4",
            expires=1_003_600,
            token=token,
            secret=SECRET,
            media_root=root,
            now=1_003_601,
        )


def test_tampered_token_and_path_are_rejected(tmp_path: Path) -> None:
    root = _make_media_file(tmp_path)
    good_token = sign_media_link("videos/石桥铭苑6-1102.mp4", 1_003_600, SECRET)

    with pytest.raises(ValueError, match="signature"):
        verify_signed_media_request(
            rel_path="videos/石桥铭苑6-1102.mp4",
            expires=1_003_600,
            token=good_token[:-1] + ("0" if good_token[-1] != "0" else "1"),
            secret=SECRET,
            media_root=root,
            now=1_000_100,
        )
    with pytest.raises(ValueError, match="signature"):
        verify_signed_media_request(
            rel_path="videos/华丰新苑20-1-504.mp4",
            expires=1_003_600,
            token=good_token,
            secret=SECRET,
            media_root=root,
            now=1_000_100,
        )


def test_traversal_and_absolute_paths_are_rejected() -> None:
    with pytest.raises(ValueError, match="traversal"):
        sign_media_link("videos/../../.env", 1_003_600, SECRET)
    with pytest.raises(ValueError, match="relative"):
        sign_media_link("/etc/passwd", 1_003_600, SECRET)
    with pytest.raises(ValueError, match="relative"):
        sign_media_link("", 1_003_600, SECRET)


def test_backslash_paths_normalize_to_same_signature() -> None:
    forward = sign_media_link("videos/a.mp4", 1_003_600, SECRET)
    backward = sign_media_link("videos\\a.mp4", 1_003_600, SECRET)
    assert forward == backward


def test_missing_file_and_outside_root_are_rejected(tmp_path: Path) -> None:
    root = _make_media_file(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        verify_signed_media_request(
            rel_path="videos/不存在.mp4",
            expires=1_003_600,
            token=sign_media_link("videos/不存在.mp4", 1_003_600, SECRET),
            secret=SECRET,
            media_root=root,
            now=1_000_100,
        )


def test_empty_secret_is_rejected() -> None:
    with pytest.raises(ValueError, match="secret"):
        sign_media_link("videos/a.mp4", 1_003_600, "")
    with pytest.raises(ValueError, match="secret"):
        sign_media_link("videos/a.mp4", 1_003_600, "   ")
