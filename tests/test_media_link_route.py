# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException

import app.main as main
from app.services.media_link_signer import build_signed_media_url


def _make_room_database(tmp_path: Path) -> Path:
    root = tmp_path / "room_database"
    (root / "videos").mkdir(parents=True)
    (root / "videos" / "石桥铭苑6-1102.mp4").write_bytes(b"original-video-bytes")
    return root


def _patch_link_settings(monkeypatch: pytest.MonkeyPatch, root: Path, *, secret: str = "route-test-secret") -> None:
    monkeypatch.setattr(main.settings, "kf_media_link_secret", secret)
    monkeypatch.setattr(main.settings, "public_base_url", "https://ynzyqbot.cn")
    monkeypatch.setattr(main.settings, "room_database_path", root)
    monkeypatch.setattr(main.settings, "kf_media_link_ttl_seconds", 3600)


def test_signed_original_video_urls_builds_links_inside_room_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_room_database(tmp_path)
    _patch_link_settings(monkeypatch, root)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")

    urls = main._signed_original_video_urls(
        [root / "videos" / "石桥铭苑6-1102.mp4", outside]
    )

    # 越界文件被跳过,只有 room_database 内的文件出链接
    assert len(urls) == 1
    parsed = urlparse(urls[0])
    assert parsed.scheme == "https"
    assert parsed.netloc == "ynzyqbot.cn"
    assert parsed.path == "/wecom/media/original"
    query = parse_qs(parsed.query)
    assert query["file"] == ["videos/石桥铭苑6-1102.mp4"]
    assert query["token"][0]
    assert int(query["expires"][0]) > 0


def test_signed_original_video_urls_disabled_without_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_room_database(tmp_path)
    _patch_link_settings(monkeypatch, root, secret="")

    assert main._signed_original_video_urls([root / "videos" / "石桥铭苑6-1102.mp4"]) == []


def test_download_original_media_roundtrip_and_rejections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_room_database(tmp_path)
    _patch_link_settings(monkeypatch, root)
    url = build_signed_media_url(
        public_base_url="https://ynzyqbot.cn",
        rel_path="videos/石桥铭苑6-1102.mp4",
        secret="route-test-secret",
        ttl_seconds=3600,
    )
    query = parse_qs(urlparse(url).query)

    response = asyncio.run(
        main.download_original_media(
            file=query["file"][0],
            expires=query["expires"][0],
            token=query["token"][0],
        )
    )
    assert Path(response.path) == (root / "videos" / "石桥铭苑6-1102.mp4").resolve()

    # 篡改 token -> 404
    with pytest.raises(HTTPException) as tampered:
        asyncio.run(
            main.download_original_media(
                file=query["file"][0],
                expires=query["expires"][0],
                token="0" * 64,
            )
        )
    assert tampered.value.status_code == 404

    # 目录穿越 -> 404
    with pytest.raises(HTTPException) as traversal:
        asyncio.run(
            main.download_original_media(
                file="../.env",
                expires=query["expires"][0],
                token=query["token"][0],
            )
        )
    assert traversal.value.status_code == 404


def test_download_original_media_disabled_without_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_room_database(tmp_path)
    _patch_link_settings(monkeypatch, root, secret="")

    with pytest.raises(HTTPException) as closed:
        asyncio.run(
            main.download_original_media(
                file="videos/石桥铭苑6-1102.mp4",
                expires="9999999999",
                token="deadbeef",
            )
        )
    assert closed.value.status_code == 404
