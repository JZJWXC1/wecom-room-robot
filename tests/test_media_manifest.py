from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.services.inventory_snapshot_models import generate_listing_id
from app.services.media_manifest import (
    BINDING_METHOD_LISTING_ID,
    MEDIA_KIND_IMAGE,
    MEDIA_KIND_ORIGINAL_VIDEO,
    MEDIA_KIND_VIDEO,
    MEDIA_VARIANT_ORIGINAL_VIDEO,
    MEDIA_VARIANT_WECOM_VIDEO,
    FeishuDriveMediaManifestAdapter,
    MediaManifest,
    MediaManifestShadowAdapter,
)


class FakeDriveClient:
    def __init__(self, tree: dict[str, list[dict[str, Any]]], payloads: dict[str, bytes]) -> None:
        self.tree = tree
        self.payloads = payloads
        self.downloaded_tokens: list[str] = []

    async def list_folder_files(self, folder_token: str) -> list[dict[str, Any]]:
        return list(self.tree.get(folder_token, []))

    async def download_file(self, file_token: str, target_path: Path) -> Path:
        self.downloaded_tokens.append(file_token)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(self.payloads[file_token])
        return target_path


def folder(name: str, token: str) -> dict[str, str]:
    return {"name": name, "type": "folder", "token": token}


def file_item(name: str, token: str, payloads: dict[str, bytes], **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "type": "file",
        "token": token,
        "size": len(payloads[token]),
        **extra,
    }


class MediaManifestFoundationTests(unittest.IsolatedAsyncioTestCase):
    async def test_shadow_adapter_exposes_listing_media_id_local_path_sha_and_utf8_name(self) -> None:
        listing_id = generate_listing_id("寓你花园", "1-101A")
        payloads = {"video-token": "中文视频内容".encode("utf-8")}
        tree = {
            "root": [folder(f"{listing_id} 素材", "listing-folder")],
            "listing-folder": [
                file_item(
                    "客厅视频.mp4",
                    "video-token",
                    payloads,
                    modified_at="2026-06-27T08:00:00Z",
                )
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            target_root = Path(directory) / "room_database"
            adapter = FeishuDriveMediaManifestAdapter(
                client=FakeDriveClient(tree, payloads),
                listing_ids=[listing_id],
                target_root=target_root,
            )

            manifest, report = await adapter.sync_from_drive(root_folder_token="root")
            shadow = MediaManifestShadowAdapter(manifest, local_root=target_root)
            evidence = shadow.evidence_for_listing(listing_id)

            self.assertEqual(report.fuzzy_candidates, [])
            self.assertEqual(len(evidence), 1)
            item = evidence[0]
            expected_sha = hashlib.sha256(payloads["video-token"]).hexdigest()
            expected_token_hash = hashlib.sha256("video-token".encode("utf-8")).hexdigest()
            self.assertEqual(item.listing_id, listing_id)
            self.assertEqual(item.variant, MEDIA_VARIANT_WECOM_VIDEO)
            self.assertEqual(item.sha256, expected_sha)
            self.assertEqual(item.source_file_token, expected_token_hash)
            self.assertNotEqual(item.source_file_token, "video-token")
            self.assertEqual(item.modified_at, "2026-06-27T08:00:00Z")
            self.assertEqual(item.binding_method, BINDING_METHOD_LISTING_ID)
            self.assertTrue(item.access_verified)
            self.assertTrue(Path(item.local_path).is_file())
            self.assertIn("客厅视频.mp4", item.local_path)
            self.assertEqual(shadow.evidence_by_media_id(item.media_id), item)

    async def test_same_listing_binds_multiple_media_and_reuses_local_files(self) -> None:
        listing_id = generate_listing_id("测试花园", "1-101A")
        payloads = {
            "img-token": b"image",
            "video-token": b"wecom-video",
            "original-token": b"original-video",
        }
        tree = {
            "root": [folder(f"{listing_id} 测试素材", "listing-folder")],
            "listing-folder": [
                file_item("客厅.jpg", "img-token", payloads),
                file_item("微信可发送.mp4", "video-token", payloads),
                folder("原视频", "original-folder"),
            ],
            "original-folder": [
                file_item(
                    "高清原视频.mov",
                    "original-token",
                    payloads,
                    url="https://media.example.invalid/original.mov",
                    material_page_url="https://docs.example.invalid/material",
                )
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            target_root = Path(directory) / "room_database"
            manifest_path = target_root / "media_manifest.json"
            client = FakeDriveClient(tree, payloads)
            adapter = FeishuDriveMediaManifestAdapter(
                client=client,
                listing_ids=[listing_id],
                target_root=target_root,
            )

            manifest, report = await adapter.sync_from_drive(
                root_folder_token="root",
                expected_kinds=[MEDIA_KIND_IMAGE, MEDIA_KIND_VIDEO, MEDIA_KIND_ORIGINAL_VIDEO],
                manifest_path=manifest_path,
            )
            second_manifest, second_report = await adapter.sync_from_drive(
                root_folder_token="root",
                expected_kinds=[MEDIA_KIND_IMAGE, MEDIA_KIND_VIDEO, MEDIA_KIND_ORIGINAL_VIDEO],
            )

            self.assertEqual(len(manifest.images_for_listing(listing_id)), 1)
            self.assertEqual(len(manifest.videos_for_listing(listing_id)), 1)
            self.assertEqual(len(manifest.original_videos_for_listing(listing_id)), 1)
            self.assertTrue(manifest.has_wecom_sendable_video(listing_id))
            self.assertTrue(manifest.has_original_video(listing_id))
            self.assertEqual(report.missing, [])
            self.assertEqual(len(report.downloaded), 3)
            self.assertEqual(second_report.downloaded, [])
            self.assertEqual(len(second_report.reused), 3)
            self.assertEqual(client.downloaded_tokens, ["img-token", "video-token", "original-token"])

            original = manifest.original_videos_for_listing(listing_id)[0]
            self.assertEqual(original.original_url, "https://media.example.invalid/original.mov")
            self.assertEqual(original.material_page_url, "https://docs.example.invalid/material")
            self.assertEqual(MediaManifest.read_json(manifest_path).source_hash, manifest.source_hash)
            self.assertEqual(second_manifest.source_hash, manifest.source_hash)

    async def test_missing_media_report_lists_listing_and_kind(self) -> None:
        listing_with_video = generate_listing_id("测试花园", "1-101A")
        listing_without_media = generate_listing_id("测试花园", "1-102A")
        payloads = {"video-token": b"video"}
        tree = {
            "root": [folder(listing_with_video, "listing-folder")],
            "listing-folder": [file_item("微信可发送.mp4", "video-token", payloads)],
        }

        with tempfile.TemporaryDirectory() as directory:
            adapter = FeishuDriveMediaManifestAdapter(
                client=FakeDriveClient(tree, payloads),
                listing_ids=[listing_with_video, listing_without_media],
                target_root=Path(directory),
            )

            _manifest, report = await adapter.sync_from_drive(
                root_folder_token="root",
                expected_kinds=[MEDIA_KIND_IMAGE, MEDIA_KIND_VIDEO],
            )

            missing = {item["listing_id"]: item["missing_kinds"] for item in report.missing}
            self.assertEqual(missing[listing_with_video], [MEDIA_KIND_IMAGE])
            self.assertEqual(
                missing[listing_without_media],
                [MEDIA_KIND_IMAGE, MEDIA_KIND_VIDEO],
            )

    async def test_ambiguous_directory_is_isolated_and_not_bound(self) -> None:
        first_listing = generate_listing_id("测试花园", "1-101A")
        second_listing = generate_listing_id("测试花园", "1-102A")
        payloads = {"video-token": b"ambiguous-video"}
        tree = {
            "root": [folder(f"{first_listing}_{second_listing}_混合素材", "mixed-folder")],
            "mixed-folder": [file_item("视频.mp4", "video-token", payloads)],
        }

        with tempfile.TemporaryDirectory() as directory:
            target_root = Path(directory)
            adapter = FeishuDriveMediaManifestAdapter(
                client=FakeDriveClient(tree, payloads),
                listing_ids=[first_listing, second_listing],
                target_root=target_root,
            )

            manifest, report = await adapter.sync_from_drive(root_folder_token="root")

            self.assertEqual(manifest.videos_for_listing(first_listing), [])
            self.assertEqual(manifest.videos_for_listing(second_listing), [])
            self.assertEqual(len(report.ambiguous_items), 1)
            self.assertEqual(
                set(report.ambiguous_items[0]["candidate_listing_ids"]),
                {first_listing, second_listing},
            )
            isolated_path = Path(report.isolated_items[0]["target_path"])
            self.assertTrue(isolated_path.is_file())
            self.assertIn("_manual_review", isolated_path.parts)

    async def test_orphan_media_with_fuzzy_candidate_only_enters_manual_report(self) -> None:
        listing_id = generate_listing_id("测试花园", "1-101A")
        payloads = {"video-token": b"orphan-video"}
        tree = {
            "root": [folder("测试花园1-101A", "fuzzy-folder")],
            "fuzzy-folder": [file_item("介绍视频.mp4", "video-token", payloads)],
        }

        with tempfile.TemporaryDirectory() as directory:
            adapter = FeishuDriveMediaManifestAdapter(
                client=FakeDriveClient(tree, payloads),
                listing_ids=[listing_id],
                listing_labels={listing_id: "测试花园1-101A"},
                target_root=Path(directory),
            )

            manifest, report = await adapter.sync_from_drive(root_folder_token="root")

            self.assertEqual(manifest.videos_for_listing(listing_id), [])
            self.assertEqual(len(report.orphan_items), 1)
            self.assertEqual(report.orphan_items[0]["candidate_listing_ids"], [])
            self.assertEqual(report.fuzzy_candidates[0]["candidates"][0]["listing_id"], listing_id)
            self.assertEqual(report.fuzzy_candidates[0]["reason"], "fuzzy_candidate_only_not_bound")
            self.assertTrue(Path(report.isolated_items[0]["target_path"]).is_file())
            self.assertEqual(
                MediaManifestShadowAdapter(manifest, local_root=Path(directory)).evidence_for_listing(listing_id),
                [],
            )

    async def test_original_video_link_exists_without_wecom_sendable_video(self) -> None:
        listing_with_original = generate_listing_id("测试花园", "1-101A")
        listing_without_original = generate_listing_id("测试花园", "1-102A")
        tree = {
            "root": [
                folder(f"{listing_with_original} 原视频链接", "original-folder"),
                folder(listing_without_original, "empty-folder"),
            ],
            "original-folder": [
                {
                    "name": "原视频下载链接",
                    "type": "link",
                    "url": "https://media.example.invalid/source-file",
                    "material_page_url": "https://docs.example.invalid/source-page",
                }
            ],
            "empty-folder": [],
        }

        with tempfile.TemporaryDirectory() as directory:
            adapter = FeishuDriveMediaManifestAdapter(
                client=FakeDriveClient(tree, {}),
                listing_ids=[listing_with_original, listing_without_original],
                target_root=Path(directory),
            )

            manifest, report = await adapter.sync_from_drive(
                root_folder_token="root",
                expected_kinds=[MEDIA_KIND_ORIGINAL_VIDEO],
            )

            self.assertTrue(manifest.has_original_video(listing_with_original))
            self.assertFalse(manifest.has_original_video(listing_without_original))
            self.assertFalse(manifest.has_wecom_sendable_video(listing_with_original))
            self.assertEqual(manifest.original_videos_for_listing(listing_with_original)[0].relative_path, "")
            self.assertEqual(
                manifest.original_videos_for_listing(listing_with_original)[0].variant,
                MEDIA_VARIANT_ORIGINAL_VIDEO,
            )
            self.assertEqual(
                manifest.original_videos_for_listing(listing_with_original)[0].original_url,
                "https://media.example.invalid/source-file",
            )
            self.assertEqual(
                MediaManifestShadowAdapter(manifest, local_root=Path(directory))
                .evidence_for_listing(listing_with_original)[0]
                .source_url,
                "https://media.example.invalid/source-file",
            )
            self.assertEqual(report.downloaded, [])
            self.assertEqual(
                report.missing,
                [{"listing_id": listing_without_original, "missing_kinds": [MEDIA_KIND_ORIGINAL_VIDEO]}],
            )

    async def test_wecom_sendable_video_exists_independently_from_original_video(self) -> None:
        listing_with_wecom = generate_listing_id("测试花园", "1-101A")
        listing_with_original_only = generate_listing_id("测试花园", "1-102A")
        payloads = {
            "wecom-token": b"wecom-video",
            "original-token": b"original-video",
        }
        tree = {
            "root": [
                folder(f"{listing_with_wecom} 普通视频", "wecom-folder"),
                folder(f"{listing_with_original_only} 原视频", "original-folder"),
            ],
            "wecom-folder": [file_item("微信可发送.mp4", "wecom-token", payloads)],
            "original-folder": [file_item("高清原视频.mp4", "original-token", payloads)],
        }

        with tempfile.TemporaryDirectory() as directory:
            adapter = FeishuDriveMediaManifestAdapter(
                client=FakeDriveClient(tree, payloads),
                listing_ids=[listing_with_wecom, listing_with_original_only],
                target_root=Path(directory),
            )

            manifest, _report = await adapter.sync_from_drive(root_folder_token="root")

            self.assertTrue(manifest.has_wecom_sendable_video(listing_with_wecom))
            self.assertFalse(manifest.has_original_video(listing_with_wecom))
            self.assertFalse(manifest.has_wecom_sendable_video(listing_with_original_only))
            self.assertTrue(manifest.has_original_video(listing_with_original_only))

    async def test_unknown_listing_id_is_reported_as_orphan_not_bound(self) -> None:
        known_listing = generate_listing_id("测试花园", "1-101A")
        unknown_listing = generate_listing_id("测试花园", "9-909Z")
        payloads = {"video-token": b"unknown-video"}
        tree = {
            "root": [folder(unknown_listing, "unknown-folder")],
            "unknown-folder": [file_item("视频.mp4", "video-token", payloads)],
        }

        with tempfile.TemporaryDirectory() as directory:
            adapter = FeishuDriveMediaManifestAdapter(
                client=FakeDriveClient(tree, payloads),
                listing_ids=[known_listing],
                target_root=Path(directory),
            )

            manifest, report = await adapter.sync_from_drive(root_folder_token="root")

            self.assertEqual(manifest.videos_for_listing(known_listing), [])
            self.assertEqual(report.ambiguous_items, [])
            self.assertEqual(report.orphan_items[0]["reason"], "unknown_listing_id")
            self.assertEqual(report.orphan_items[0]["candidate_listing_ids"], [unknown_listing])


if __name__ == "__main__":
    unittest.main()
