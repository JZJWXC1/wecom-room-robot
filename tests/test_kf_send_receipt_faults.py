import hashlib
import asyncio
import json
import logging
import tempfile
from pathlib import Path

import app.main as main
from app.services import kf_outbox


def _context() -> dict:
    return {
        "structured_memory": {
            "current_turn_id": "turn-video",
            "turn_records": [{"turn_id": "turn-video", "msgids": ["msg-video-fail"]}],
        }
    }


def test_video_send_failure_records_failed_receipt_and_allows_replay() -> None:
    class FakeWeComKf:
        def __init__(self) -> None:
            self.fail_video_upload = True
            self.texts: list[str] = []
            self.uploads: list[str] = []
            self.videos: list[str] = []

        def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
            self.texts.append(text)
            return {"errcode": 0}

        def upload_media(self, path: Path, media_type: str = "video") -> str:
            if self.fail_video_upload:
                raise RuntimeError("upload failed token=abc123 phone 19900009999")
            self.uploads.append(str(path))
            return f"media-{len(self.uploads)}"

        def send_video_media(self, open_kfid: str, external_userid: str, media_id: str) -> dict:
            self.videos.append(str(media_id))
            return {"errcode": 0, "msgid": f"video-{len(self.videos)}"}

    async def run_case() -> tuple[dict, dict, FakeWeComKf, str]:
        fake = FakeWeComKf()
        original_wecom = main.wecom_kf
        main.wecom_kf = fake
        try:
            with tempfile.TemporaryDirectory() as directory:
                video_path = str(Path(directory) / "room.mp4")
                Path(video_path).write_bytes(b"video")
                first = await main._send_final_actions(
                    open_kfid="kf",
                    external_userid="wm",
                    context=_context(),
                    final_reply="",
                    tool_evidence={
                        "video_paths": [video_path],
                        "video_rows": [{"小区": "星河苑", "房号": "1-101"}],
                    },
                    msgids=["msg-video-fail"],
                )
                fake.fail_video_upload = False
                second = await main._send_final_actions(
                    open_kfid="kf",
                    external_userid="wm",
                    context=first["context"],
                    final_reply="",
                    tool_evidence={
                        "video_paths": [video_path],
                        "video_rows": [{"小区": "星河苑", "房号": "1-101"}],
                    },
                    msgids=["msg-video-fail"],
                )
                return first, second, fake, video_path
        finally:
            main.wecom_kf = original_wecom

    first, second, fake, video_path = asyncio.run(run_case())

    # 上传失败时不得出现"这是XX的视频。"孤儿话术，只允许失败纠正话术
    assert first["sent_actions"][0]["type"] == "video_failed"
    assert first["sent_actions"][1] == {"type": "text", "subtype": "video_send_failure_notice", "count": 1}
    assert second["sent_actions"] == [{"type": "video", "path": video_path, "room": "星河苑1-101", "count": 1}]
    assert fake.uploads == [video_path]
    assert fake.videos == ["media-1"]
    assert fake.texts == [
        "星河苑1-101的视频这边暂时没发出去，你稍后再让我发一次。",
        "这是星河苑1-101的视频。",
    ]
    statuses = [item["status"] for item in second["context"]["send_receipts"]["receipts"]]
    # 第一轮：视频上传确定失败 + 纠正话术；第二轮重放：话术与视频均成功
    assert statuses == ["failed", "sent", "sent", "sent"]
    dumped = json.dumps(second["context"], ensure_ascii=False)
    assert "abc123" not in dumped
    assert "19900009999" not in dumped


def test_video_upload_failure_transcodes_with_ffmpeg_and_retries() -> None:
    class FakeWeComKf:
        def __init__(self) -> None:
            self.texts: list[str] = []
            self.video_attempts: list[str] = []
            self.videos: list[str] = []

        def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
            self.texts.append(text)
            return {"errcode": 0}

        def upload_media(self, path: Path, media_type: str = "video") -> str:
            self.video_attempts.append(str(path))
            if len(self.video_attempts) == 1:
                raise RuntimeError("video upload failed: file too large")
            return f"media-{len(self.video_attempts)}"

        def send_video_media(self, open_kfid: str, external_userid: str, media_id: str) -> dict:
            self.videos.append(str(media_id))
            return {"errcode": 0, "msgid": "video-transcoded"}

    async def run_case() -> tuple[dict, FakeWeComKf, list[str], str, str]:
        fake = FakeWeComKf()
        transcode_calls: list[str] = []
        original_wecom = main.wecom_kf
        original_prepare = main.prepare_wecom_video
        main.wecom_kf = fake
        try:
            with tempfile.TemporaryDirectory() as directory:
                video_path = Path(directory) / "room.mp4"
                transcoded_path = Path(directory) / "room.wecom.mp4"
                video_path.write_bytes(b"original-video")
                transcoded_path.write_bytes(b"transcoded-video")
                listing_id = "lst_1234567890abcdef"
                evidence_id = "evd_1234567890abcdef"
                snapshot_id = "snapshot-test-001"
                source_hash = "a" * 64

                def fake_prepare(path: Path, *, force: bool = False, **kwargs) -> Path:
                    assert force is True
                    transcode_calls.append(str(path))
                    return transcoded_path

                main.prepare_wecom_video = fake_prepare
                result = await main._send_final_actions(
                    open_kfid="kf",
                    external_userid="wm",
                    context=_context(),
                    final_reply="",
                    tool_evidence={
                        "video_paths": [str(video_path)],
                        "video_rows": [{"小区": "星河苑", "房号": "1-101", "listing_id": listing_id}],
                        "inventory_listing_evidence": [
                            {
                                "listing_id": listing_id,
                                "evidence_id": evidence_id,
                                "snapshot_id": snapshot_id,
                                "source_hash": source_hash,
                            }
                        ],
                    },
                    msgids=["msg-video-transcode"],
                )
                return result, fake, transcode_calls, str(video_path), str(transcoded_path)
        finally:
            main.wecom_kf = original_wecom
            main.prepare_wecom_video = original_prepare

    result, fake, transcode_calls, video_path, transcoded_path = asyncio.run(run_case())

    assert transcode_calls == [video_path]
    assert fake.video_attempts == [video_path, transcoded_path]
    assert fake.videos == ["media-2"]
    # 口径变更(批10):上传超限且无转码缓存时,先发转码等待提示,
    # 再转码重传;caption 仍在上传成功之后、视频消息之前发出。
    assert fake.texts == [
        "视频有点大，正在压缩，请稍等。",
        "这是星河苑1-101的视频。",
    ]
    assert result["sent_actions"] == [
        {
            "type": "video",
            "path": transcoded_path,
            "room": "星河苑1-101",
            "count": 1,
            "source_path": video_path,
            "transcode_retry": True,
        }
    ]
    receipt = result["context"]["send_receipts"]["receipts"][-1]
    assert receipt["status"] == "sent"
    assert receipt["listing_id"] == "lst_1234567890abcdef"
    assert receipt["evidence_id"] == "evd_1234567890abcdef"
    assert receipt["inventory_snapshot_id"] == "snapshot-test-001"
    assert receipt["source_hash"] == "a" * 64
    assert receipt["sha256"] == hashlib.sha256(b"original-video").hexdigest()
    metadata = receipt["metadata"]
    assert metadata["transcode_retry"] is True
    assert metadata["outbox_attempt"] == 1
    assert metadata["failure_stage"] == ""
    assert metadata["transcode_cache_hit"] is False
    assert metadata["original_file_name"] == "room.mp4"
    assert metadata["sent_file_name"] == "room.wecom.mp4"
    assert metadata["original_file_size_bytes"] == len(b"original-video")
    assert metadata["sent_file_size_bytes"] == len(b"transcoded-video")
    for key in ("first_upload_ms", "transcode_ms", "retry_upload_ms", "video_msg_ms", "send_total_ms"):
        assert isinstance(metadata[key], int)
        assert metadata[key] >= 0
    dumped_receipt = json.dumps(receipt, ensure_ascii=False)
    assert Path(video_path).parent.as_posix() not in dumped_receipt


def test_successful_transcode_send_blocks_duplicate_callback_without_recompressing(tmp_path) -> None:
    class FakeWeComKf:
        def __init__(self) -> None:
            self.texts: list[str] = []
            self.video_attempts: list[str] = []
            self.videos: list[str] = []

        def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
            self.texts.append(text)
            return {"errcode": 0}

        def upload_media(self, path: Path, media_type: str = "video") -> str:
            self.video_attempts.append(str(path))
            if len(self.video_attempts) == 1:
                raise RuntimeError("video upload failed: file too large")
            return f"media-{len(self.video_attempts)}"

        def send_video_media(self, open_kfid: str, external_userid: str, media_id: str) -> dict:
            self.videos.append(str(media_id))
            return {"errcode": 0, "msgid": "video-transcoded"}

    async def run_case() -> tuple[dict, dict, FakeWeComKf, list[str], str, str]:
        fake = FakeWeComKf()
        transcode_calls: list[str] = []
        original_wecom = main.wecom_kf
        original_prepare = main.prepare_wecom_video
        original_outbox = main.kf_send_outbox
        main.wecom_kf = fake
        main.kf_send_outbox = kf_outbox.LocalKfOutboxLedger(tmp_path / "kf_send_outbox.jsonl")
        try:
            video_path = tmp_path / "room.mp4"
            transcoded_path = tmp_path / "room.wecom.mp4"
            video_path.write_bytes(b"original-video")
            transcoded_path.write_bytes(b"transcoded-video")

            def fake_prepare(path: Path, *, force: bool = False, **kwargs) -> Path:
                assert force is True
                transcode_calls.append(str(path))
                return transcoded_path

            main.prepare_wecom_video = fake_prepare
            first = await main._send_final_actions(
                open_kfid="kf",
                external_userid="wm",
                context={
                    "structured_memory": {
                        "current_turn_id": "turn-video-1",
                        "turn_records": [{"turn_id": "turn-video-1", "msgids": ["msg-video-transcode-once"]}],
                    }
                },
                final_reply="",
                tool_evidence={
                    "video_paths": [str(video_path)],
                    "video_rows": [{"小区": "星河苑", "房号": "1-101"}],
                },
                msgids=["msg-video-transcode-once"],
            )
            second = await main._send_final_actions(
                open_kfid="kf",
                external_userid="wm",
                context={
                    "structured_memory": {
                        "current_turn_id": "turn-video-restarted",
                        "turn_records": [{"turn_id": "turn-video-restarted", "msgids": ["msg-video-transcode-once"]}],
                    }
                },
                final_reply="",
                tool_evidence={
                    "video_paths": [str(video_path)],
                    "video_rows": [{"小区": "星河苑", "房号": "1-101"}],
                },
                msgids=["msg-video-transcode-once"],
            )
            return first, second, fake, transcode_calls, str(video_path), str(transcoded_path)
        finally:
            main.wecom_kf = original_wecom
            main.prepare_wecom_video = original_prepare
            main.kf_send_outbox = original_outbox

    first, second, fake, transcode_calls, video_path, transcoded_path = asyncio.run(run_case())

    assert first["sent_actions"] == [
        {
            "type": "video",
            "path": transcoded_path,
            "room": "星河苑1-101",
            "count": 1,
            "source_path": video_path,
            "transcode_retry": True,
        }
    ]
    assert second["sent_actions"] == []
    assert fake.video_attempts == [video_path, transcoded_path]
    # 口径变更(批10):首轮=转码等待提示+caption 两条;重复回调重放不得新增任何文本。
    assert fake.texts == [
        "视频有点大，正在压缩，请稍等。",
        "这是星河苑1-101的视频。",
    ]
    assert transcode_calls == [video_path]
    assert second["context"]["send_receipts"]["receipts"][-1]["status"] == "skipped_duplicate"


def test_transcode_cache_hit_skips_wait_notice() -> None:
    # 口径(批10):转码缓存命中时秒回,不发"正在压缩,请稍等"提示,避免无谓打扰。
    class FakeWeComKf:
        def __init__(self) -> None:
            self.texts: list[str] = []
            self.video_attempts: list[str] = []
            self.videos: list[str] = []

        def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
            self.texts.append(text)
            return {"errcode": 0}

        def upload_media(self, path: Path, media_type: str = "video") -> str:
            self.video_attempts.append(str(path))
            if len(self.video_attempts) == 1:
                raise RuntimeError("video upload failed: invalid video size")
            return f"media-{len(self.video_attempts)}"

        def send_video_media(self, open_kfid: str, external_userid: str, media_id: str) -> dict:
            self.videos.append(str(media_id))
            return {"errcode": 0, "msgid": "video-cached"}

    async def run_case() -> tuple[dict, FakeWeComKf]:
        fake = FakeWeComKf()
        original_wecom = main.wecom_kf
        original_prepare = main.prepare_wecom_video
        main.wecom_kf = fake
        try:
            with tempfile.TemporaryDirectory() as directory:
                video_path = Path(directory) / "room.mp4"
                video_path.write_bytes(b"original-video")
                cache_dir = Path(directory) / ".wecom_cache"
                cache_dir.mkdir()
                cached_path = cache_dir / "room.wecom.mp4"
                cached_path.write_bytes(b"cached-video")

                def fake_prepare(path: Path, *, force: bool = False, **kwargs) -> Path:
                    return cached_path

                main.prepare_wecom_video = fake_prepare
                result = await main._send_final_actions(
                    open_kfid="kf",
                    external_userid="wm",
                    context=_context(),
                    final_reply="",
                    tool_evidence={
                        "video_paths": [str(video_path)],
                        "video_rows": [{"小区": "星河苑", "房号": "1-101"}],
                    },
                    msgids=["msg-video-cache-hit"],
                )
                return result, fake
        finally:
            main.wecom_kf = original_wecom
            main.prepare_wecom_video = original_prepare

    result, fake = asyncio.run(run_case())

    assert fake.texts == ["这是星河苑1-101的视频。"]
    receipt = result["context"]["send_receipts"]["receipts"][-1]
    assert receipt["metadata"]["transcode_cache_hit"] is True
    assert receipt["status"] == "sent"


def test_video_auth_failure_does_not_transcode_retry(caplog) -> None:
    class FakeWeComKf:
        def __init__(self) -> None:
            self.texts: list[str] = []
            self.video_attempts: list[str] = []

        def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
            self.texts.append(text)
            return {"errcode": 0}

        def upload_media(self, path: Path, media_type: str = "video") -> str:
            self.video_attempts.append(str(path))
            raise RuntimeError("video send failed: invalid credential access_token")

        def send_video_media(self, open_kfid: str, external_userid: str, media_id: str) -> dict:
            raise AssertionError("upload failed, video message must not be attempted")

    async def run_case() -> tuple[dict, FakeWeComKf, list[str], str]:
        fake = FakeWeComKf()
        transcode_calls: list[str] = []
        original_wecom = main.wecom_kf
        original_prepare = main.prepare_wecom_video
        main.wecom_kf = fake
        try:
            with tempfile.TemporaryDirectory() as directory:
                video_path = Path(directory) / "room.mp4"
                video_path.write_bytes(b"original-video")

                def fake_prepare(path: Path, *, force: bool = False, **kwargs) -> Path:
                    transcode_calls.append(str(path))
                    return Path(directory) / "room.wecom.mp4"

                main.prepare_wecom_video = fake_prepare
                result = await main._send_final_actions(
                    open_kfid="kf",
                    external_userid="wm",
                    context=_context(),
                    final_reply="",
                    tool_evidence={
                        "video_paths": [str(video_path)],
                        "video_rows": [{"小区": "星河苑", "房号": "1-101"}],
                    },
                    msgids=["msg-video-auth-failure"],
                )
                return result, fake, transcode_calls, str(video_path)
        finally:
            main.wecom_kf = original_wecom
            main.prepare_wecom_video = original_prepare

    caplog.set_level(logging.WARNING, logger="room-robot")
    result, fake, transcode_calls, video_path = asyncio.run(run_case())

    assert transcode_calls == []
    assert fake.video_attempts == [video_path]
    assert result["sent_actions"][0]["type"] == "video_failed"
    # 上传失败无孤儿话术，只补发纠正话术
    assert fake.texts == ["星河苑1-101的视频这边暂时没发出去，你稍后再让我发一次。"]
    assert result["sent_actions"][1] == {"type": "text", "subtype": "video_send_failure_notice", "count": 1}
    receipt = next(item for item in result["context"]["send_receipts"]["receipts"] if item["status"] == "failed")
    metadata = receipt["metadata"]
    assert metadata["transcode_retry"] is False
    assert metadata["outbox_attempt"] == 1
    assert metadata["failure_stage"] == "first_upload"
    assert metadata["first_upload_ms"] >= 0
    assert metadata["transcode_ms"] is None
    assert metadata["retry_upload_ms"] is None
    assert metadata["video_msg_ms"] is None
    assert metadata["send_total_ms"] >= 0
    dumped = json.dumps(result["context"], ensure_ascii=False)
    assert "access_token" not in dumped
    assert "access_token" not in caplog.text


def test_video_rate_limit_upload_failure_does_not_transcode_retry(caplog) -> None:
    class FakeWeComKf:
        def __init__(self) -> None:
            self.texts: list[str] = []
            self.video_attempts: list[str] = []

        def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
            self.texts.append(text)
            return {"errcode": 0}

        def upload_media(self, path: Path, media_type: str = "video") -> str:
            self.video_attempts.append(str(path))
            raise RuntimeError("temporary media upload failed: 429 rate limit token=token_CANARY_abcdefghijklmnopqrstuvwxyz")

        def send_video_media(self, open_kfid: str, external_userid: str, media_id: str) -> dict:
            raise AssertionError("upload failed, video message must not be attempted")

    async def run_case() -> tuple[dict, FakeWeComKf, list[str], str]:
        fake = FakeWeComKf()
        transcode_calls: list[str] = []
        original_wecom = main.wecom_kf
        original_prepare = main.prepare_wecom_video
        main.wecom_kf = fake
        try:
            with tempfile.TemporaryDirectory() as directory:
                video_path = Path(directory) / "room.mp4"
                video_path.write_bytes(b"original-video")

                def fake_prepare(path: Path, *, force: bool = False, **kwargs) -> Path:
                    transcode_calls.append(str(path))
                    return Path(directory) / "room.wecom.mp4"

                main.prepare_wecom_video = fake_prepare
                result = await main._send_final_actions(
                    open_kfid="kf",
                    external_userid="wm",
                    context=_context(),
                    final_reply="",
                    tool_evidence={
                        "video_paths": [str(video_path)],
                        "video_rows": [{"小区": "星河苑", "房号": "1-101"}],
                    },
                    msgids=["msg-video-rate-limit"],
                )
                return result, fake, transcode_calls, str(video_path)
        finally:
            main.wecom_kf = original_wecom
            main.prepare_wecom_video = original_prepare

    caplog.set_level(logging.WARNING, logger="room-robot")
    result, fake, transcode_calls, video_path = asyncio.run(run_case())

    assert transcode_calls == []
    assert fake.video_attempts == [video_path]
    assert result["sent_actions"][0]["type"] == "video_failed"
    assert fake.texts == ["星河苑1-101的视频这边暂时没发出去，你稍后再让我发一次。"]
    receipt = next(item for item in result["context"]["send_receipts"]["receipts"] if item["status"] == "failed")
    metadata = receipt["metadata"]
    assert metadata["transcode_retry"] is False
    assert metadata["outbox_attempt"] == 1
    assert metadata["failure_stage"] == "first_upload"
    assert metadata["first_upload_ms"] >= 0
    assert metadata["transcode_ms"] is None
    assert metadata["retry_upload_ms"] is None
    assert metadata["video_msg_ms"] is None
    assert metadata["send_total_ms"] >= 0
    dumped = json.dumps(result["context"], ensure_ascii=False)
    assert "token_CANARY" not in dumped
    assert "token_CANARY" not in caplog.text


def test_video_daily_limit_exceeded_does_not_transcode_retry() -> None:
    class FakeWeComKf:
        def __init__(self) -> None:
            self.texts: list[str] = []
            self.video_attempts: list[str] = []

        def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
            self.texts.append(text)
            return {"errcode": 0}

        def upload_media(self, path: Path, media_type: str = "video") -> str:
            self.video_attempts.append(str(path))
            raise RuntimeError("api call daily limit exceeded")

        def send_video_media(self, open_kfid: str, external_userid: str, media_id: str) -> dict:
            raise AssertionError("upload failed, video message must not be attempted")

    async def run_case() -> tuple[dict, FakeWeComKf, list[str], str]:
        fake = FakeWeComKf()
        transcode_calls: list[str] = []
        original_wecom = main.wecom_kf
        original_prepare = main.prepare_wecom_video
        main.wecom_kf = fake
        try:
            with tempfile.TemporaryDirectory() as directory:
                video_path = Path(directory) / "room.mp4"
                video_path.write_bytes(b"original-video")

                def fake_prepare(path: Path, *, force: bool = False, **kwargs) -> Path:
                    transcode_calls.append(str(path))
                    return Path(directory) / "room.wecom.mp4"

                main.prepare_wecom_video = fake_prepare
                result = await main._send_final_actions(
                    open_kfid="kf",
                    external_userid="wm",
                    context=_context(),
                    final_reply="",
                    tool_evidence={
                        "video_paths": [str(video_path)],
                        "video_rows": [{"小区": "星河苑", "房号": "1-101"}],
                    },
                    msgids=["msg-video-daily-limit"],
                )
                return result, fake, transcode_calls, str(video_path)
        finally:
            main.wecom_kf = original_wecom
            main.prepare_wecom_video = original_prepare

    result, fake, transcode_calls, video_path = asyncio.run(run_case())

    assert transcode_calls == []
    assert fake.video_attempts == [video_path]
    assert result["sent_actions"][0]["type"] == "video_failed"
    receipt = next(item for item in result["context"]["send_receipts"]["receipts"] if item["status"] == "failed")
    assert receipt["metadata"]["transcode_retry"] is False


def test_persistent_outbox_blocks_duplicate_text_after_context_loss(tmp_path) -> None:
    class FakeWeComKf:
        def __init__(self) -> None:
            self.texts: list[str] = []

        def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
            self.texts.append(text)
            return {"errcode": 0, "msgid": f"text-{len(self.texts)}"}

    async def run_case() -> tuple[dict, dict, FakeWeComKf, list[dict]]:
        fake = FakeWeComKf()
        original_wecom = main.wecom_kf
        original_outbox = main.kf_send_outbox
        main.wecom_kf = fake
        main.kf_send_outbox = kf_outbox.LocalKfOutboxLedger(tmp_path / "kf_send_outbox.jsonl")
        try:
            first = await main._send_final_actions(
                open_kfid="kf",
                external_userid="wm",
                context={
                    "structured_memory": {
                        "current_turn_id": "turn-text-1",
                        "turn_records": [{"turn_id": "turn-text-1", "msgids": ["msg-text-persist"]}],
                    }
                },
                final_reply="这条我已经发过一次。",
                tool_evidence={"suppress_actions": True},
                msgids=["msg-text-persist"],
            )
            second = await main._send_final_actions(
                open_kfid="kf",
                external_userid="wm",
                context={
                    "structured_memory": {
                        "current_turn_id": "turn-text-restarted",
                        "turn_records": [{"turn_id": "turn-text-restarted", "msgids": ["msg-text-persist"]}],
                    }
                },
                final_reply="这条我已经发过一次。",
                tool_evidence={"suppress_actions": True},
                msgids=["msg-text-persist"],
            )
            return first, second, fake, main.kf_send_outbox.records()
        finally:
            main.wecom_kf = original_wecom
            main.kf_send_outbox = original_outbox

    first, second, fake, records = asyncio.run(run_case())

    assert first["sent_actions"] == [{"type": "text", "count": 1}]
    assert second["sent_actions"] == []
    assert fake.texts == ["这条我已经发过一次。"]
    assert second["context"]["send_receipts"]["receipts"][-1]["status"] == "skipped_duplicate"
    assert [record["status"] for record in records if record["record_type"] == "receipt"] == [
        "sent",
        "skipped_duplicate",
    ]


def test_uncertain_video_result_blocks_automatic_replay_after_context_loss(tmp_path) -> None:
    class FakeWeComKf:
        def __init__(self) -> None:
            self.texts: list[str] = []
            self.video_attempts: list[str] = []

        def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
            self.texts.append(text)
            return {"errcode": 0}

        def upload_media(self, path: Path, media_type: str = "video") -> str:
            self.video_attempts.append(str(path))
            return f"media-{len(self.video_attempts)}"

        def send_video_media(self, open_kfid: str, external_userid: str, media_id: str) -> dict:
            raise TimeoutError("send timed out after request body was written")

    async def run_case() -> tuple[dict, dict, FakeWeComKf, list[dict], str]:
        fake = FakeWeComKf()
        original_wecom = main.wecom_kf
        original_outbox = main.kf_send_outbox
        main.wecom_kf = fake
        main.kf_send_outbox = kf_outbox.LocalKfOutboxLedger(tmp_path / "kf_send_outbox.jsonl")
        try:
            video_path = tmp_path / "room.mp4"
            video_path.write_bytes(b"video")
            first = await main._send_final_actions(
                open_kfid="kf",
                external_userid="wm",
                context={
                    "structured_memory": {
                        "current_turn_id": "turn-video-1",
                        "turn_records": [{"turn_id": "turn-video-1", "msgids": ["msg-video-timeout"]}],
                    }
                },
                final_reply="",
                tool_evidence={
                    "video_paths": [str(video_path)],
                    "video_rows": [{"小区": "星河苑", "房号": "1-101"}],
                },
                msgids=["msg-video-timeout"],
            )
            second = await main._send_final_actions(
                open_kfid="kf",
                external_userid="wm",
                context={
                    "structured_memory": {
                        "current_turn_id": "turn-video-restarted",
                        "turn_records": [{"turn_id": "turn-video-restarted", "msgids": ["msg-video-timeout"]}],
                    }
                },
                final_reply="",
                tool_evidence={
                    "video_paths": [str(video_path)],
                    "video_rows": [{"小区": "星河苑", "房号": "1-101"}],
                },
                msgids=["msg-video-timeout"],
            )
            return first, second, fake, main.kf_send_outbox.records(), str(video_path)
        finally:
            main.wecom_kf = original_wecom
            main.kf_send_outbox = original_outbox

    first, second, fake, records, video_path = asyncio.run(run_case())

    assert first["sent_actions"] == [
        {
            "type": "video_failed",
            "path": video_path,
            "room": "星河苑1-101",
            "reason": "send timed out after request body was written",
        }
    ]
    assert second["sent_actions"] == []
    assert fake.video_attempts == [video_path]
    # 视频消息未决时只有 caption 一条文本，禁止追加"没发出去"类纠正话术（消息可能已送达）
    assert fake.texts == ["这是星河苑1-101的视频。"]
    assert first["context"]["send_receipts"]["receipts"][-1]["status"] == "send_uncertain"
    assert second["context"]["send_receipts"]["receipts"][-1]["status"] == "skipped_duplicate"
    assert [record["status"] for record in records if record["record_type"] == "receipt"] == [
        "sent",
        "send_uncertain",
        "skipped_duplicate",
    ]


def test_video_message_definite_failure_after_caption_sends_corrective_notice(tmp_path) -> None:
    class FakeWeComKf:
        def __init__(self) -> None:
            self.texts: list[str] = []
            self.uploads: list[str] = []
            self.fail_video_message = True
            self.videos: list[str] = []

        def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
            self.texts.append(text)
            return {"errcode": 0, "msgid": f"text-{len(self.texts)}"}

        def upload_media(self, path: Path, media_type: str = "video") -> str:
            self.uploads.append(str(path))
            return f"media-{len(self.uploads)}"

        def send_video_media(self, open_kfid: str, external_userid: str, media_id: str) -> dict:
            if self.fail_video_message:
                raise RuntimeError("kf send_msg rejected: errcode 95001")
            self.videos.append(str(media_id))
            return {"errcode": 0, "msgid": f"video-{len(self.videos)}"}

    async def run_case() -> tuple[dict, dict, FakeWeComKf, str]:
        fake = FakeWeComKf()
        original_wecom = main.wecom_kf
        original_outbox = main.kf_send_outbox
        main.wecom_kf = fake
        main.kf_send_outbox = kf_outbox.LocalKfOutboxLedger(tmp_path / "kf_send_outbox.jsonl")
        try:
            video_path = tmp_path / "room.mp4"
            video_path.write_bytes(b"video")
            first = await main._send_final_actions(
                open_kfid="kf",
                external_userid="wm",
                context=_context(),
                final_reply="",
                tool_evidence={
                    "video_paths": [str(video_path)],
                    "video_rows": [{"小区": "星河苑", "房号": "1-101"}],
                },
                msgids=["msg-video-msg-fail"],
            )
            fake.fail_video_message = False
            second = await main._send_final_actions(
                open_kfid="kf",
                external_userid="wm",
                context=first["context"],
                final_reply="",
                tool_evidence={
                    "video_paths": [str(video_path)],
                    "video_rows": [{"小区": "星河苑", "房号": "1-101"}],
                },
                msgids=["msg-video-msg-fail"],
            )
            return first, second, fake, str(video_path)
        finally:
            main.wecom_kf = original_wecom
            main.kf_send_outbox = original_outbox

    first, second, fake, video_path = asyncio.run(run_case())

    # caption 已发出而视频消息确定失败：必须补发纠正话术修正孤儿话术
    assert fake.texts[0] == "这是星河苑1-101的视频。"
    assert fake.texts[1] == "星河苑1-101的视频这边暂时没发出去，你稍后再让我发一次。"
    assert first["sent_actions"][0]["type"] == "video_failed"
    assert first["sent_actions"][1] == {"type": "text", "subtype": "video_send_failure_notice", "count": 1}
    failed_receipt = next(item for item in first["context"]["send_receipts"]["receipts"] if item["status"] == "failed")
    assert failed_receipt["metadata"]["failure_stage"] == "video_message"
    assert failed_receipt["metadata"]["caption_sent"] is True
    assert failed_receipt["metadata"]["video_msg_ms"] >= 0
    # 确定失败允许重放：第二轮视频补发成功，caption 与纠正话术不重复外发
    assert second["sent_actions"] == [{"type": "video", "path": video_path, "room": "星河苑1-101", "count": 1}]
    assert fake.videos == ["media-2"]
    assert len(fake.texts) == 2


def test_video_upload_timeout_is_definite_failure_and_allows_replay(tmp_path) -> None:
    class FakeWeComKf:
        def __init__(self) -> None:
            self.texts: list[str] = []
            self.uploads: list[str] = []
            self.fail_upload = True
            self.videos: list[str] = []

        def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
            self.texts.append(text)
            return {"errcode": 0, "msgid": f"text-{len(self.texts)}"}

        def upload_media(self, path: Path, media_type: str = "video") -> str:
            self.uploads.append(str(path))
            if self.fail_upload:
                raise TimeoutError("send timed out after request body was written")
            return f"media-{len(self.uploads)}"

        def send_video_media(self, open_kfid: str, external_userid: str, media_id: str) -> dict:
            self.videos.append(str(media_id))
            return {"errcode": 0, "msgid": f"video-{len(self.videos)}"}

    async def run_case() -> tuple[dict, dict, FakeWeComKf, list[dict], str]:
        fake = FakeWeComKf()
        original_wecom = main.wecom_kf
        original_outbox = main.kf_send_outbox
        main.wecom_kf = fake
        main.kf_send_outbox = kf_outbox.LocalKfOutboxLedger(tmp_path / "kf_send_outbox.jsonl")
        try:
            video_path = tmp_path / "room.mp4"
            video_path.write_bytes(b"video")
            first = await main._send_final_actions(
                open_kfid="kf",
                external_userid="wm",
                context=_context(),
                final_reply="",
                tool_evidence={
                    "video_paths": [str(video_path)],
                    "video_rows": [{"小区": "星河苑", "房号": "1-101"}],
                },
                msgids=["msg-video-upload-timeout"],
            )
            fake.fail_upload = False
            second = await main._send_final_actions(
                open_kfid="kf",
                external_userid="wm",
                context=first["context"],
                final_reply="",
                tool_evidence={
                    "video_paths": [str(video_path)],
                    "video_rows": [{"小区": "星河苑", "房号": "1-101"}],
                },
                msgids=["msg-video-upload-timeout"],
            )
            return first, second, fake, main.kf_send_outbox.records(), str(video_path)
        finally:
            main.wecom_kf = original_wecom
            main.kf_send_outbox = original_outbox

    first, second, fake, records, video_path = asyncio.run(run_case())

    # 上传阶段的超时不等于消息送达未决：视频消息必然没发出，按确定失败处理
    failed_receipt = next(item for item in first["context"]["send_receipts"]["receipts"] if item["status"] == "failed")
    assert failed_receipt["metadata"]["failure_stage"] == "first_upload"
    assert all(item["status"] != "send_uncertain" for item in first["context"]["send_receipts"]["receipts"])
    assert first["sent_actions"][0]["type"] == "video_failed"
    assert first["sent_actions"][1] == {"type": "text", "subtype": "video_send_failure_notice", "count": 1}
    # 确定失败允许自动重放，第二轮补发成功
    assert second["sent_actions"] == [{"type": "video", "path": video_path, "room": "星河苑1-101", "count": 1}]
    assert fake.uploads == [video_path, video_path]
    assert fake.videos == ["media-2"]
    assert fake.texts == [
        "星河苑1-101的视频这边暂时没发出去，你稍后再让我发一次。",
        "这是星河苑1-101的视频。",
    ]


def test_send_final_actions_legacy_dedups_merged_reply_into_video_captions(tmp_path) -> None:
    class FakeWeComKf:
        def __init__(self) -> None:
            self.texts: list[str] = []
            self.uploads: list[str] = []
            self.videos: list[str] = []

        def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
            self.texts.append(text)
            return {"errcode": 0, "msgid": f"text-{len(self.texts)}"}

        def upload_media(self, path: Path, media_type: str = "video") -> str:
            self.uploads.append(str(path))
            return f"media-{len(self.uploads)}"

        def send_video_media(self, open_kfid: str, external_userid: str, media_id: str) -> dict:
            self.videos.append(str(media_id))
            return {"errcode": 0, "msgid": f"video-{len(self.videos)}"}

    async def run_case() -> tuple[dict, FakeWeComKf, dict]:
        fake = FakeWeComKf()
        original_wecom = main.wecom_kf
        main.wecom_kf = fake
        try:
            first_path = tmp_path / "room-1.mp4"
            second_path = tmp_path / "room-2.mp4"
            first_path.write_bytes(b"video-1")
            second_path.write_bytes(b"video-2")
            tool_evidence = {
                "video_paths": [str(first_path), str(second_path)],
                "video_rows": [
                    {"小区": "星河苑", "房号": "1-101"},
                    {"小区": "星河苑", "房号": "2-202"},
                ],
            }
            result = await main._send_final_actions(
                open_kfid="kf",
                external_userid="wm",
                context=_context(),
                final_reply="这是星河苑1-101的视频，这是星河苑2-202的视频。",
                tool_evidence=tool_evidence,
                msgids=["msg-legacy-dedup"],
            )
            return result, fake, tool_evidence
        finally:
            main.wecom_kf = original_wecom

    result, fake, tool_evidence = asyncio.run(run_case())

    # 生产实证形态：合并版话术逐子句均被 caption 覆盖时不再单独外发，一次请求只剩逐条 caption
    assert tool_evidence["final_reply_deduped_into_captions"] is True
    assert fake.texts == [
        "这是星河苑1-101的视频。",
        "这是星河苑2-202的视频。",
    ]
    assert [action["type"] for action in result["sent_actions"]] == ["video", "video"]


def test_send_final_actions_legacy_keeps_reply_not_covered_by_captions(tmp_path) -> None:
    class FakeWeComKf:
        def __init__(self) -> None:
            self.texts: list[str] = []
            self.uploads: list[str] = []
            self.videos: list[str] = []

        def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
            self.texts.append(text)
            return {"errcode": 0, "msgid": f"text-{len(self.texts)}"}

        def upload_media(self, path: Path, media_type: str = "video") -> str:
            self.uploads.append(str(path))
            return f"media-{len(self.uploads)}"

        def send_video_media(self, open_kfid: str, external_userid: str, media_id: str) -> dict:
            self.videos.append(str(media_id))
            return {"errcode": 0, "msgid": f"video-{len(self.videos)}"}

    async def run_case() -> tuple[dict, FakeWeComKf, dict]:
        fake = FakeWeComKf()
        original_wecom = main.wecom_kf
        main.wecom_kf = fake
        try:
            video_path = tmp_path / "room.mp4"
            video_path.write_bytes(b"video")
            tool_evidence = {
                "video_paths": [str(video_path)],
                "video_rows": [{"小区": "星河苑", "房号": "1-101"}],
            }
            result = await main._send_final_actions(
                open_kfid="kf",
                external_userid="wm",
                context=_context(),
                final_reply="这是星河苑1-101的视频，押一付一1900。",
                tool_evidence=tool_evidence,
                msgids=["msg-legacy-keep"],
            )
            return result, fake, tool_evidence
        finally:
            main.wecom_kf = original_wecom

    result, fake, tool_evidence = asyncio.run(run_case())

    # 合并话术含 caption 之外的事实（价格）时必须照常发送，不允许静默丢内容
    assert "final_reply_deduped_into_captions" not in tool_evidence
    assert fake.texts == [
        "这是星河苑1-101的视频，押一付一1900。",
        "这是星河苑1-101的视频。",
    ]
    assert [action["type"] for action in result["sent_actions"]] == ["text", "video"]


def test_final_reply_caption_clause_coverage_rules() -> None:
    covered = main._final_reply_fully_covered_by_media_captions
    captions = ["这是星河苑1-101的视频。", "这是星河苑2-202的视频。"]

    # 逗号合并、句号分列、标点/空白差异均视为覆盖
    assert covered("这是星河苑1-101的视频，这是星河苑2-202的视频。", captions) is True
    assert covered("这是星河苑1-101的视频。这是星河苑2-202的视频。", captions) is True
    assert covered(" 这是星河苑1-101的视频 ", captions) is True
    # 任何一个子句带新信息即不覆盖；空 caption 集不触发去重
    assert covered("这是星河苑1-101的视频，押一付一1900。", captions) is False
    assert covered("好的，这就发你。", captions) is False
    assert covered("这是星河苑1-101的视频。", []) is False
    assert covered("", captions) is False
