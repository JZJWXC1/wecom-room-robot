import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app.services.media_store import MediaStore


class MediaStoreVideoMatchingTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
