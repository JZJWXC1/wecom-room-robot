import asyncio
import json
import tempfile
from pathlib import Path

import app.main as main


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
    assert statuses == ["failed", "sent"]
    dumped = json.dumps(second["context"], ensure_ascii=False)
    assert "abc123" not in dumped
    assert "19900009999" not in dumped
