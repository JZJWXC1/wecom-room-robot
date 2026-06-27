from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.services.inventory_snapshot_models import generate_listing_id
from app.services.media_manifest import (
    BINDING_METHOD_LISTING_ID,
    BINDING_METHOD_FUZZY_FILENAME,
    MEDIA_MANIFEST_SCHEMA_VERSION,
    MEDIA_KIND_IMAGE,
    MEDIA_KIND_ORIGINAL_VIDEO,
    MEDIA_KIND_VIDEO,
    MEDIA_SOURCE_KIND_ORIGINAL_VIDEO_FILE,
    MEDIA_SOURCE_KIND_ORIGINAL_VIDEO_LINK,
    MEDIA_SOURCE_KIND_WECOM_VIDEO_FILE,
    MEDIA_VARIANT_ORIGINAL_VIDEO,
    MEDIA_VARIANT_WECOM_VIDEO,
    FeishuDriveMediaManifestAdapter,
    MediaItem,
    MediaManifest,
    MediaManifestProductionAdapter,
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
            self.assertEqual(item.media_type, MEDIA_KIND_VIDEO)
            self.assertEqual(item.variant, MEDIA_VARIANT_WECOM_VIDEO)
            self.assertEqual(item.source_kind, MEDIA_SOURCE_KIND_WECOM_VIDEO_FILE)
            self.assertEqual(len(item.source_path_hash), 64)
            self.assertEqual(item.source_record_id, expected_token_hash)
            self.assertEqual(item.confidence, 1.0)
            self.assertFalse(item.ambiguity)
            self.assertFalse(item.candidate_only)
            self.assertTrue(item.send_ready)
            self.assertEqual(item.manifest_version, MEDIA_MANIFEST_SCHEMA_VERSION)
            self.assertEqual(item.sha256, expected_sha)
            self.assertEqual(item.source_file_token, expected_token_hash)
            self.assertNotEqual(item.source_file_token, "video-token")
            self.assertEqual(item.modified_at, "2026-06-27T08:00:00Z")
            self.assertEqual(item.binding_method, BINDING_METHOD_LISTING_ID)
            self.assertTrue(item.access_verified)
            self.assertTrue(Path(item.local_path).is_file())
            self.assertIn("客厅视频.mp4", item.local_path)
            self.assertEqual(shadow.evidence_by_media_id(item.media_id), item)
            self.assertEqual(report.bound_items[0]["source_kind"], MEDIA_SOURCE_KIND_WECOM_VIDEO_FILE)
            self.assertTrue(report.bound_items[0]["send_ready"])

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
            self.assertEqual(original.media_type, MEDIA_KIND_ORIGINAL_VIDEO)
            self.assertEqual(original.source_kind, MEDIA_SOURCE_KIND_ORIGINAL_VIDEO_FILE)
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
            self.assertTrue(report.ambiguous_items[0]["ambiguity"])
            self.assertTrue(report.ambiguous_items[0]["candidate_only"])
            self.assertFalse(report.ambiguous_items[0]["send_ready"])
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
            self.assertEqual(report.fuzzy_candidates[0]["binding_method"], BINDING_METHOD_FUZZY_FILENAME)
            self.assertTrue(report.fuzzy_candidates[0]["ambiguity"])
            self.assertTrue(report.fuzzy_candidates[0]["candidate_only"])
            self.assertFalse(report.fuzzy_candidates[0]["send_ready"])
            self.assertLess(report.fuzzy_candidates[0]["confidence"], 1.0)
            self.assertTrue(report.fuzzy_candidates[0]["candidates"][0]["candidate_only"])
            self.assertFalse(report.fuzzy_candidates[0]["candidates"][0]["send_ready"])
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
                manifest.original_videos_for_listing(listing_with_original)[0].source_kind,
                MEDIA_SOURCE_KIND_ORIGINAL_VIDEO_LINK,
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

    async def test_cross_room_filename_listing_id_blocks_binding(self) -> None:
        first_listing = generate_listing_id("寓你花园", "1-101A")
        second_listing = generate_listing_id("寓你花园", "1-102A")
        payloads = {"video-token": b"cross-room-video"}
        tree = {
            "root": [folder(f"{first_listing} 寓你花园素材", "first-folder")],
            "first-folder": [file_item(f"{second_listing}_客厅视频.mp4", "video-token", payloads)],
        }

        with tempfile.TemporaryDirectory() as directory:
            adapter = FeishuDriveMediaManifestAdapter(
                client=FakeDriveClient(tree, payloads),
                listing_ids=[first_listing, second_listing],
                target_root=Path(directory),
            )

            manifest, report = await adapter.sync_from_drive(root_folder_token="root")

            self.assertEqual(manifest.videos_for_listing(first_listing), [])
            self.assertEqual(manifest.videos_for_listing(second_listing), [])
            self.assertEqual(len(report.ambiguous_items), 1)
            self.assertEqual(
                set(report.ambiguous_items[0]["candidate_listing_ids"]),
                {first_listing, second_listing},
            )
            self.assertTrue(report.ambiguous_items[0]["candidate_only"])
            self.assertFalse(report.ambiguous_items[0]["send_ready"])

    async def test_chinese_paths_and_filenames_round_trip_as_utf8(self) -> None:
        listing_id = generate_listing_id("寓你花园", "1-101A")
        file_name = "客厅阳台视频.mp4"
        payloads = {"cn-token": "中文视频内容".encode("utf-8")}
        tree = {
            "root": [folder(f"{listing_id} 寓你花园素材", "listing-folder")],
            "listing-folder": [file_item(file_name, "cn-token", payloads)],
        }

        with tempfile.TemporaryDirectory() as directory:
            target_root = Path(directory) / "room_database"
            manifest_path = target_root / "media_manifest.json"
            adapter = FeishuDriveMediaManifestAdapter(
                client=FakeDriveClient(tree, payloads),
                listing_ids=[listing_id],
                target_root=target_root,
            )

            manifest, _report = await adapter.sync_from_drive(
                root_folder_token="root",
                manifest_path=manifest_path,
            )
            text = manifest_path.read_text(encoding="utf-8")
            data = json.loads(text)
            evidence = MediaManifestShadowAdapter(manifest, local_root=target_root).evidence_for_listing(listing_id)

            self.assertIn(file_name, text)
            self.assertEqual(data["items"][0]["file_name"], file_name)
            self.assertEqual(evidence[0].file_name, file_name)
            self.assertTrue(Path(evidence[0].local_path).is_file())

    async def test_candidate_only_manifest_item_never_becomes_send_ready_evidence(self) -> None:
        listing_id = generate_listing_id("寓你花园", "1-101A")
        item = MediaItem(
            listing_id=listing_id,
            kind=MEDIA_KIND_VIDEO,
            file_name="候选视频.mp4",
            relative_path="video/candidate/候选视频.mp4",
            binding_method=BINDING_METHOD_FUZZY_FILENAME,
            confidence=0.8,
            ambiguity=True,
            candidate_only=True,
        )
        manifest = MediaManifest.build(listing_ids=[listing_id], items=[item])
        shadow = MediaManifestShadowAdapter(manifest, local_root=Path("."))
        production = MediaManifestProductionAdapter(manifest, local_root=Path("."))

        self.assertEqual(shadow.evidence_for_listing(listing_id), [])
        self.assertIsNone(shadow.evidence_by_media_id(item.media_id))
        self.assertEqual(production.evidence_for_listing(listing_id), [])
        self.assertIsNone(production.evidence_by_media_id(item.media_id))
        self.assertIsNone(production.local_file_for_media_id(item.media_id))

    async def test_production_adapter_exposes_only_exact_listing_id_bound_media(self) -> None:
        listing_id = generate_listing_id("寓你花园", "1-101A")
        exact_item = MediaItem(
            listing_id=listing_id,
            kind=MEDIA_KIND_VIDEO,
            file_name="精确绑定视频.mp4",
            relative_path="video/listing/精确绑定视频.mp4",
            binding_method=BINDING_METHOD_LISTING_ID,
            confidence=1.0,
            ambiguity=False,
            candidate_only=False,
            access_verified=True,
        )
        fuzzy_item = MediaItem(
            listing_id=listing_id,
            kind=MEDIA_KIND_VIDEO,
            file_name="模糊候选视频.mp4",
            relative_path="video/candidate/模糊候选视频.mp4",
            binding_method=BINDING_METHOD_FUZZY_FILENAME,
            confidence=0.8,
            ambiguity=True,
            candidate_only=True,
        )
        manifest = MediaManifest.build(listing_ids=[listing_id], items=[exact_item, fuzzy_item])
        production = MediaManifestProductionAdapter(manifest, local_root=Path("."))

        evidence = production.evidence_for_listing(listing_id)

        self.assertEqual([item.media_id for item in evidence], [exact_item.media_id])
        self.assertTrue(evidence[0].send_ready)
        self.assertEqual(evidence[0].adapter_mode, "production_read")
        self.assertEqual(evidence[0].evidence_profile, "media_manifest.production_read.v1")
        self.assertIsNone(production.evidence_by_media_id(fuzzy_item.media_id))
        self.assertIsNone(production.local_file_for_media_id(fuzzy_item.media_id))

    async def test_production_adapter_exposes_only_exact_listing_id_bound_media(self) -> None:
        listing_id = generate_listing_id("Unit Garden", "1-101A")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exact_file = root / "video" / listing_id / "exact.mp4"
            exact_file.parent.mkdir(parents=True)
            exact_file.write_bytes(b"exact-video")
            exact_sha = hashlib.sha256(b"exact-video").hexdigest()
            exact_item = MediaItem(
                listing_id=listing_id,
                kind=MEDIA_KIND_VIDEO,
                file_name=exact_file.name,
                relative_path=f"video/{listing_id}/{exact_file.name}",
                sha256=exact_sha,
                binding_method=BINDING_METHOD_LISTING_ID,
                confidence=1.0,
                ambiguity=False,
                candidate_only=False,
                access_verified=True,
            )
            fuzzy_item = MediaItem(
                listing_id=listing_id,
                kind=MEDIA_KIND_VIDEO,
                file_name="candidate.mp4",
                relative_path="video/candidate/candidate.mp4",
                binding_method=BINDING_METHOD_FUZZY_FILENAME,
                confidence=0.8,
                ambiguity=True,
                candidate_only=True,
            )
            manifest = MediaManifest.build(listing_ids=[listing_id], items=[exact_item, fuzzy_item])
            production = MediaManifestProductionAdapter(manifest, local_root=root)

            evidence = production.evidence_for_listing(listing_id)

            self.assertEqual([item.media_id for item in evidence], [exact_item.media_id])
            self.assertTrue(evidence[0].send_ready)
            self.assertTrue(evidence[0].access_verified)
            self.assertEqual(evidence[0].sha256, exact_sha)
            self.assertEqual(evidence[0].adapter_mode, "production_read")
            self.assertEqual(evidence[0].evidence_profile, "media_manifest.production_read.v1")
            self.assertIsNone(production.evidence_by_media_id(fuzzy_item.media_id))
            self.assertIsNone(production.local_file_for_media_id(fuzzy_item.media_id))

    async def test_production_adapter_rejects_missing_file_and_hash_mismatch(self) -> None:
        listing_id = generate_listing_id("Unit Garden", "1-101A")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mismatch_file = root / "video" / listing_id / "mismatch.mp4"
            mismatch_file.parent.mkdir(parents=True)
            mismatch_file.write_bytes(b"actual-video")
            mismatch_item = MediaItem(
                listing_id=listing_id,
                kind=MEDIA_KIND_VIDEO,
                file_name=mismatch_file.name,
                relative_path=f"video/{listing_id}/{mismatch_file.name}",
                sha256=hashlib.sha256(b"expected-video").hexdigest(),
                binding_method=BINDING_METHOD_LISTING_ID,
                confidence=1.0,
                ambiguity=False,
                candidate_only=False,
            )
            missing_item = MediaItem(
                listing_id=listing_id,
                kind=MEDIA_KIND_VIDEO,
                file_name="missing.mp4",
                relative_path=f"video/{listing_id}/missing.mp4",
                sha256=hashlib.sha256(b"missing-video").hexdigest(),
                binding_method=BINDING_METHOD_LISTING_ID,
                confidence=1.0,
                ambiguity=False,
                candidate_only=False,
            )
            manifest = MediaManifest.build(
                listing_ids=[listing_id],
                items=[mismatch_item, missing_item],
            )
            production = MediaManifestProductionAdapter(manifest, local_root=root)

            self.assertEqual(production.evidence_for_listing(listing_id), [])
            self.assertIsNone(production.evidence_by_media_id(mismatch_item.media_id))
            self.assertIsNone(production.local_file_for_media_id(missing_item.media_id))

    async def test_manifest_safe_serialization_hashes_source_record_fields(self) -> None:
        listing_id = generate_listing_id("寓你花园", "1-101A")
        raw_token = "raw-file-token-for-test"
        raw_record_id = "raw-record-for-test"
        raw_path_hash = "raw-path-for-test"
        item = MediaItem(
            listing_id=listing_id,
            kind=MEDIA_KIND_VIDEO,
            file_name="客厅视频.mp4",
            relative_path="video/listing/客厅视频.mp4",
            source_path="寓你花园/1-101A/客厅视频.mp4",
            source_file_token=raw_token,
            source_record_id=raw_record_id,
            source_path_hash=raw_path_hash,
        )
        manifest = MediaManifest.build(
            listing_ids=[listing_id],
            items=[item],
            generated_at="2026-06-27T08:00:00Z",
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "media_manifest.json"
            manifest.write_json(path)
            text = path.read_text(encoding="utf-8")
            data = json.loads(text)

            self.assertNotIn(raw_token, text)
            self.assertNotIn(raw_record_id, text)
            self.assertNotIn(raw_path_hash, text)
            stored = data["items"][0]
            self.assertEqual(
                stored["source_file_token"],
                hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(
                stored["source_record_id"],
                hashlib.sha256(raw_record_id.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(
                stored["source_path_hash"],
                hashlib.sha256(raw_path_hash.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(stored["media_type"], MEDIA_KIND_VIDEO)
            self.assertEqual(stored["manifest_version"], MEDIA_MANIFEST_SCHEMA_VERSION)

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
