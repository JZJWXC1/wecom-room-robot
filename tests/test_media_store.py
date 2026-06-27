import json
import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app.services.inventory_snapshot_models import generate_listing_id
from app.services.media_manifest import (
    BINDING_METHOD_FUZZY_FILENAME,
    MEDIA_KIND_VIDEO,
    MEDIA_SOURCE_KIND_WECOM_VIDEO_FILE,
    MediaItem,
    MediaManifest,
)
from app.services.media_store import MediaStore


class MediaStoreVideoMatchingTests(unittest.TestCase):
    def test_media_manifest_evidence_is_read_only_and_does_not_replace_legacy_matching(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous_room_database_path = settings.room_database_path
            try:
                settings.room_database_path = Path(directory) / "room_database"
                legacy_dir = settings.room_database_path / "video" / "寓你花园1-101A"
                legacy_dir.mkdir(parents=True)
                legacy_video = legacy_dir / "旧发送路径.mp4"
                legacy_video.write_bytes(b"legacy-video")

                listing_id = generate_listing_id("寓你花园", "1-101A")
                manifest_video = settings.room_database_path / "video" / listing_id / "manifest证据.mp4"
                manifest_video.parent.mkdir(parents=True)
                manifest_video.write_bytes(b"manifest-video")
                MediaManifest.build(
                    listing_ids=[listing_id],
                    items=[
                        MediaItem(
                            listing_id=listing_id,
                            kind=MEDIA_KIND_VIDEO,
                            file_name=manifest_video.name,
                            relative_path=f"video/{listing_id}/{manifest_video.name}",
                            sha256="",
                            binding_method="listing_id",
                            access_verified=True,
                        )
                    ],
                    generated_at="2026-06-27T08:00:00Z",
                ).write_json(settings.room_database_path / "media_manifest.json")

                evidence = MediaStore().media_manifest_evidence_for_listing(listing_id)
                matches = MediaStore().list_room_database_videos("寓你花园1-101A视频", limit=3)

                self.assertEqual(len(evidence), 1)
                self.assertEqual(evidence[0]["listing_id"], listing_id)
                self.assertEqual(evidence[0]["media_type"], MEDIA_KIND_VIDEO)
                self.assertEqual(evidence[0]["source_kind"], MEDIA_SOURCE_KIND_WECOM_VIDEO_FILE)
                self.assertEqual(evidence[0]["binding_method"], "listing_id")
                self.assertTrue(evidence[0]["send_ready"])
                self.assertFalse(evidence[0]["ambiguity"])
                self.assertFalse(evidence[0]["candidate_only"])
                self.assertEqual(evidence[0]["adapter_mode"], "production_read")
                self.assertEqual(evidence[0]["evidence_profile"], "media_manifest.production_read.v1")
                self.assertIn("manifest证据.mp4", evidence[0]["local_path"])
                self.assertEqual(matches, [legacy_video])
            finally:
                settings.room_database_path = previous_room_database_path

    def test_media_manifest_candidate_only_items_are_hidden_from_store_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous_room_database_path = settings.room_database_path
            try:
                settings.room_database_path = Path(directory) / "room_database"
                settings.room_database_path.mkdir(parents=True)
                listing_id = generate_listing_id("寓你花园", "1-101A")
                MediaManifest.build(
                    listing_ids=[listing_id],
                    items=[
                        MediaItem(
                            listing_id=listing_id,
                            kind=MEDIA_KIND_VIDEO,
                            file_name="候选视频.mp4",
                            relative_path="video/candidate/候选视频.mp4",
                            binding_method=BINDING_METHOD_FUZZY_FILENAME,
                            confidence=0.75,
                            ambiguity=True,
                            candidate_only=True,
                        )
                    ],
                ).write_json(settings.room_database_path / "media_manifest.json")

                evidence = MediaStore().media_manifest_evidence_for_listing(listing_id)

                self.assertEqual(evidence, [])
            finally:
                settings.room_database_path = previous_room_database_path

    def test_matches_video_request_with_quantity_words(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous_room_database_path = settings.room_database_path
            try:
                settings.room_database_path = Path(directory) / "room_database"
                video_dir_1 = settings.room_database_path / "video" / "小洋坝三区12-1003-2"
                video_dir_2 = settings.room_database_path / "video" / "小洋坝二区6-901-4"
                video_dir_1.mkdir(parents=True)
                video_dir_2.mkdir(parents=True)
                video_1 = video_dir_1 / "微信视频_1.mp4"
                video_2 = video_dir_2 / "微信视频_2.mp4"
                video_1.write_bytes(b"video")
                video_2.write_bytes(b"video")

                matches = MediaStore().list_room_database_videos(
                    "小洋坝两个视频发给我",
                    limit=3,
                )

                self.assertEqual(set(matches), {video_1, video_2})
            finally:
                settings.room_database_path = previous_room_database_path

    def test_matches_note_request_to_room_video(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous_room_database_path = settings.room_database_path
            try:
                settings.room_database_path = Path(directory) / "room_database"
                video_dir = settings.room_database_path / "video" / "永佳新苑2-703"
                video_dir.mkdir(parents=True)
                video = video_dir / "永佳新苑2-703.mp4"
                video.write_bytes(b"video")

                matches = MediaStore().list_room_database_videos(
                    "永佳新苑一室一厅703笔记发一下",
                    limit=3,
                )

                self.assertEqual(matches, [video])
            finally:
                settings.room_database_path = previous_room_database_path

    def test_ignores_wecom_transcode_cache_videos(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous_room_database_path = settings.room_database_path
            try:
                settings.room_database_path = Path(directory) / "room_database"
                video_dir = settings.room_database_path / "video" / "琬秋铭府1-1803"
                cache_dir = video_dir / ".wecom_cache"
                video_dir.mkdir(parents=True)
                cache_dir.mkdir(parents=True)
                original = video_dir / "琬秋铭府1-1803.mp4"
                cached = cache_dir / "琬秋铭府1-1803.wecom.mp4"
                original.write_bytes(b"video")
                cached.write_bytes(b"cached")

                matches = MediaStore().list_room_database_videos("琬秋铭府视频发一下", limit=3)

                self.assertEqual(matches, [original])
            finally:
                settings.room_database_path = previous_room_database_path

    def test_fuzzy_matches_typoed_community_video_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous_room_database_path = settings.room_database_path
            try:
                settings.room_database_path = Path(directory) / "room_database"
                video_dir = settings.room_database_path / "video" / "永佳新苑2-703"
                video_dir.mkdir(parents=True)
                video = video_dir / "永佳新苑2-703.mp4"
                video.write_bytes(b"video")

                matches = MediaStore().list_room_database_videos(
                    "永住新苑视频发一下",
                    limit=3,
                )

                self.assertEqual(matches, [video])
            finally:
                settings.room_database_path = previous_room_database_path

    def test_matches_abbreviated_community_with_room_number_video_query(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous_room_database_path = settings.room_database_path
            try:
                settings.room_database_path = Path(directory) / "room_database"
                video_dir = settings.room_database_path / "video" / "华丰人家8-603"
                video_dir.mkdir(parents=True)
                video = video_dir / "华丰人家8-603.mp4"
                video.write_bytes(b"video")

                matches = MediaStore().list_room_database_videos(
                    "华丰8-603有视频吗",
                    limit=3,
                )

                self.assertEqual(matches, [video])
            finally:
                settings.room_database_path = previous_room_database_path

    def test_room_number_query_does_not_match_other_room_in_same_community(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous_room_database_path = settings.room_database_path
            try:
                settings.room_database_path = Path(directory) / "room_database"
                video_dir = settings.room_database_path / "video" / "皋塘运都12-1-1802"
                video_dir.mkdir(parents=True)
                video = video_dir / "皋塘运都12-1-1802.mp4"
                video.write_bytes(b"video")

                matches = MediaStore().list_room_database_videos(
                    "皋塘运都12-2-401",
                    limit=3,
                )

                self.assertEqual(matches, [])
            finally:
                settings.room_database_path = previous_room_database_path

    def test_parent_room_match_allows_legacy_filename_room_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous_room_database_path = settings.room_database_path
            try:
                settings.room_database_path = Path(directory) / "room_database"
                video_dir = settings.room_database_path / "video" / "杨家新雅苑15-603"
                video_dir.mkdir(parents=True)
                video = video_dir / "杨家新雅苑15-1-603.mp4"
                video.write_bytes(b"video")

                matches = MediaStore().list_room_database_videos(
                    "杨家新雅苑15-603视频发我",
                    limit=3,
                )

                self.assertEqual(matches, [video])
            finally:
                settings.room_database_path = previous_room_database_path

    def test_matches_room_number_with_or_without_suffix_hyphen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous_room_database_path = settings.room_database_path
            try:
                settings.room_database_path = Path(directory) / "room_database"
                video_dir = settings.room_database_path / "video" / "金昌苑2-2-1601E"
                video_dir.mkdir(parents=True)
                video = video_dir / "cc2b5e46a7b5c9ee3c653f8c78699761.mp4"
                video.write_bytes(b"video")

                matches = MediaStore().list_room_database_videos(
                    "金昌苑2-2-1601-E视频发一下",
                    limit=3,
                )

                self.assertEqual(matches, [video])
            finally:
                settings.room_database_path = previous_room_database_path

    def test_matches_room_letter_a_with_legacy_dash_one_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous_room_database_path = settings.room_database_path
            try:
                settings.room_database_path = Path(directory) / "room_database"
                video_dir = settings.room_database_path / "video" / "孔家埭和府1-1-901A"
                video_dir.mkdir(parents=True)
                video = video_dir / "e0f3564a3e5273e1af26a6d9673e3e3b.mp4"
                video.write_bytes(b"video")

                matches = MediaStore().list_room_database_videos(
                    "孔家埭和府1-1-901-1视频发一下",
                    limit=3,
                )

                self.assertEqual(matches, [video])
            finally:
                settings.room_database_path = previous_room_database_path

    def test_matches_room_database_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous_room_database_path = settings.room_database_path
            try:
                settings.room_database_path = Path(directory) / "room_database"
                image_dir = settings.room_database_path / "images" / "小洋坝三区12-1003-2"
                image_dir.mkdir(parents=True)
                image = image_dir / "客厅.jpg"
                image.write_bytes(b"image")

                matches = MediaStore().list_room_database_images(
                    "小洋坝图片发我",
                    limit=3,
                )

                self.assertEqual(matches, [image])
            finally:
                settings.room_database_path = previous_room_database_path

    def test_reads_original_video_source_manifest_for_matched_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous_room_database_path = settings.room_database_path
            try:
                settings.room_database_path = Path(directory) / "room_database"
                video_dir = settings.room_database_path / "video" / "棠润府15-2-801B"
                video_dir.mkdir(parents=True)
                video = video_dir / "棠润府15-2-801B.mp4"
                video.write_bytes(b"video")
                (settings.room_database_path / "media_sources.json").write_text(
                    json.dumps(
                        {
                            "sources": [
                                {
                                    "path": "video/棠润府15-2-801B/棠润府15-2-801B.mp4",
                                    "original_url": "https://ccn9urs7d60k.feishu.cn/file/source-video",
                                    "material_page_url": "https://ccn9urs7d60k.feishu.cn/docx/source-doc",
                                }
                            ]
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

                sources = MediaStore().original_video_sources_for_paths([video])

                self.assertEqual(sources["original_video_urls"], ["https://ccn9urs7d60k.feishu.cn/file/source-video"])
                self.assertEqual(sources["material_page_urls"], ["https://ccn9urs7d60k.feishu.cn/docx/source-doc"])
            finally:
                settings.room_database_path = previous_room_database_path

    def test_fuzzy_matches_typoed_community_image_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous_room_database_path = settings.room_database_path
            try:
                settings.room_database_path = Path(directory) / "room_database"
                image_dir = settings.room_database_path / "images" / "华丰人家8-603"
                image_dir.mkdir(parents=True)
                image = image_dir / "客厅.jpg"
                image.write_bytes(b"image")

                matches = MediaStore().list_room_database_images(
                    "华风人家照片发我",
                    limit=3,
                )

                self.assertEqual(matches, [image])
            finally:
                settings.room_database_path = previous_room_database_path


if __name__ == "__main__":
    unittest.main()
