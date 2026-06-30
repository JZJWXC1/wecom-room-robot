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
            self.fail_video = True
            self.texts: list[str] = []
            self.videos: list[str] = []

        def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
            self.texts.append(text)
            return {"errcode": 0}

        def send_video(self, open_kfid: str, external_userid: str, media_id: str) -> dict:
            if self.fail_video:
                raise RuntimeError("upload failed token=abc123 phone 19900009999")
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
                fake.fail_video = False
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

    assert first["sent_actions"][0]["type"] == "video_failed"
    assert second["sent_actions"] == [{"type": "video", "path": video_path, "room": "星河苑1-101", "count": 1}]
    assert fake.videos == [video_path]
    statuses = [item["status"] for item in second["context"]["send_receipts"]["receipts"]]
    assert statuses == ["sent", "failed", "skipped_duplicate", "sent"]
    dumped = json.dumps(second["context"], ensure_ascii=False)
    assert "abc123" not in dumped
    assert "19900009999" not in dumped


def test_video_upload_failure_transcodes_with_ffmpeg_and_retries() -> None:
    class FakeWeComKf:
        def __init__(self) -> None:
            self.texts: list[str] = []
            self.video_attempts: list[str] = []

        def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
            self.texts.append(text)
            return {"errcode": 0}

        def send_video(self, open_kfid: str, external_userid: str, media_id: str) -> dict:
            self.video_attempts.append(str(media_id))
            if len(self.video_attempts) == 1:
                raise RuntimeError("video upload failed: file too large")
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
    for key in ("first_upload_ms", "transcode_ms", "retry_upload_ms", "send_total_ms"):
        assert isinstance(metadata[key], int)
        assert metadata[key] >= 0
    dumped_receipt = json.dumps(receipt, ensure_ascii=False)
    assert Path(video_path).parent.as_posix() not in dumped_receipt


def test_successful_transcode_send_blocks_duplicate_callback_without_recompressing(tmp_path) -> None:
    class FakeWeComKf:
        def __init__(self) -> None:
            self.texts: list[str] = []
            self.video_attempts: list[str] = []

        def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
            self.texts.append(text)
            return {"errcode": 0}

        def send_video(self, open_kfid: str, external_userid: str, media_id: str) -> dict:
            self.video_attempts.append(str(media_id))
            if len(self.video_attempts) == 1:
                raise RuntimeError("video upload failed: file too large")
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
    assert len(fake.texts) == 1
    assert transcode_calls == [video_path]
    assert second["context"]["send_receipts"]["receipts"][-1]["status"] == "skipped_duplicate"


def test_video_auth_failure_does_not_transcode_retry(caplog) -> None:
    class FakeWeComKf:
        def __init__(self) -> None:
            self.video_attempts: list[str] = []

        def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
            return {"errcode": 0}

        def send_video(self, open_kfid: str, external_userid: str, media_id: str) -> dict:
            self.video_attempts.append(str(media_id))
            raise RuntimeError("video send failed: invalid credential access_token")

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
    receipt = result["context"]["send_receipts"]["receipts"][-1]
    assert receipt["status"] == "failed"
    metadata = receipt["metadata"]
    assert metadata["transcode_retry"] is False
    assert metadata["outbox_attempt"] == 1
    assert metadata["failure_stage"] == "first_upload"
    assert metadata["first_upload_ms"] >= 0
    assert metadata["transcode_ms"] is None
    assert metadata["retry_upload_ms"] is None
    assert metadata["send_total_ms"] >= 0
    dumped = json.dumps(result["context"], ensure_ascii=False)
    assert "access_token" not in dumped
    assert "access_token" not in caplog.text


def test_video_rate_limit_upload_failure_does_not_transcode_retry(caplog) -> None:
    class FakeWeComKf:
        def __init__(self) -> None:
            self.video_attempts: list[str] = []

        def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
            return {"errcode": 0}

        def send_video(self, open_kfid: str, external_userid: str, media_id: str) -> dict:
            self.video_attempts.append(str(media_id))
            raise RuntimeError("temporary media upload failed: 429 rate limit token=token_CANARY_abcdefghijklmnopqrstuvwxyz")

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
    receipt = result["context"]["send_receipts"]["receipts"][-1]
    assert receipt["status"] == "failed"
    metadata = receipt["metadata"]
    assert metadata["transcode_retry"] is False
    assert metadata["outbox_attempt"] == 1
    assert metadata["failure_stage"] == "first_upload"
    assert metadata["first_upload_ms"] >= 0
    assert metadata["transcode_ms"] is None
    assert metadata["retry_upload_ms"] is None
    assert metadata["send_total_ms"] >= 0
    dumped = json.dumps(result["context"], ensure_ascii=False)
    assert "token_CANARY" not in dumped
    assert "token_CANARY" not in caplog.text


def test_video_daily_limit_exceeded_does_not_transcode_retry() -> None:
    class FakeWeComKf:
        def __init__(self) -> None:
            self.video_attempts: list[str] = []

        def send_text(self, open_kfid: str, external_userid: str, text: str) -> dict:
            return {"errcode": 0}

        def send_video(self, open_kfid: str, external_userid: str, media_id: str) -> dict:
            self.video_attempts.append(str(media_id))
            raise RuntimeError("api call daily limit exceeded")

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
    receipt = result["context"]["send_receipts"]["receipts"][-1]
    assert receipt["status"] == "failed"
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

        def send_video(self, open_kfid: str, external_userid: str, media_id: str) -> dict:
            self.video_attempts.append(str(media_id))
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
    assert len(fake.texts) == 1
    assert first["context"]["send_receipts"]["receipts"][-1]["status"] == "send_uncertain"
    assert second["context"]["send_receipts"]["receipts"][-1]["status"] == "skipped_duplicate"
    assert [record["status"] for record in records if record["record_type"] == "receipt"] == [
        "sent",
        "send_uncertain",
        "skipped_duplicate",
    ]
