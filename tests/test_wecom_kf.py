import asyncio
import tempfile
import unittest
from pathlib import Path

import app.main as main
from app.models import ReplyPlan
from app.services.wecom_kf import (
    WeComKfClient,
    WeComKfContextStore,
    WeComKfSendLimitError,
    WeComKfStateStore,
    extract_kf_text,
    is_kf_message_event,
    should_auto_reply_kf_message,
)


class WeComKfStateStoreTests(unittest.TestCase):
    def test_persists_cursor_and_processed_msgids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = WeComKfStateStore(path=path, max_msgids=2)

            store.save_cursor("cursor-1")
            store.mark_processed("msg-1")
            store.mark_processed("msg-2")
            store.mark_processed("msg-3")

            self.assertEqual(store.load()["cursor"], "cursor-1")
            self.assertFalse(store.is_processed("msg-1"))
            self.assertTrue(store.is_processed("msg-2"))
            self.assertTrue(store.is_processed("msg-3"))


class WeComKfContextStoreTests(unittest.TestCase):
    def test_persists_media_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            store = WeComKfContextStore(path=path)

            store.save(
                "kf_xxx:wm_xxx",
                {
                    "image_paths": [Path("room_database/inventory_1.png")],
                    "video_paths": [Path("room_database/video/x/video.mp4")],
                    "video_urls": ["https://example.com/video.mp4"],
                    "updated_at": 123.0,
                },
            )

            self.assertEqual(
                store.get("kf_xxx:wm_xxx"),
                {
                    "image_paths": [str(Path("room_database/inventory_1.png"))],
                    "video_paths": [str(Path("room_database/video/x/video.mp4"))],
                    "video_urls": ["https://example.com/video.mp4"],
                    "recent_messages": [],
                    "updated_at": 123.0,
                },
            )

    def test_keeps_recent_ten_dialog_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            store = WeComKfContextStore(path=path)

            store.save(
                "kf_xxx:wm_xxx",
                {
                    "recent_messages": [
                        {"role": "客户", "content": f"消息{index}", "created_at": float(index)}
                        for index in range(12)
                    ],
                    "updated_at": 123.0,
                },
            )

            messages = store.get("kf_xxx:wm_xxx")["recent_messages"]
            self.assertEqual(len(messages), 10)
            self.assertEqual(messages[0]["content"], "消息2")
            self.assertEqual(messages[-1]["content"], "消息11")


class WeComKfMediaSearchTextTests(unittest.TestCase):
    def test_generic_video_followup_uses_latest_assistant_room_detail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous_store = main.wecom_kf_context_store
            try:
                main.wecom_kf_context_store = WeComKfContextStore(
                    path=Path(directory) / "context.json"
                )
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_context_store.save(
                    "kf_xxx:wm_xxx",
                    {
                        "recent_messages": [
                            {"role": "客户", "content": "皋塘还有房子吗？"},
                            {"role": "客服", "content": "皋塘运都16-1-1003还有。"},
                            {"role": "客户", "content": "我要小样坝的"},
                            {
                                "role": "客服",
                                "content": (
                                    "小样坝这边我查到一套：\n"
                                    "小区：小洋坝家园\n"
                                    "房号：二区6-801-3\n"
                                    "户型：一室独立厨卫带阳台"
                                ),
                            },
                            {"role": "客户", "content": "视频发我一下吧"},
                        ],
                        "updated_at": 9999999999.0,
                    },
                )

                search_text = main._kf_media_search_text(
                    "kf_xxx",
                    "wm_xxx",
                    "视频发我一下吧",
                )

                self.assertIn("小洋坝家园", search_text)
                self.assertIn("二区6-801-3", search_text)
                self.assertNotIn("皋塘", search_text)
            finally:
                main.wecom_kf_context_store = previous_store
                main.wecom_kf_conversation_memory.clear()

    def test_explicit_video_query_does_not_reuse_old_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous_store = main.wecom_kf_context_store
            try:
                main.wecom_kf_context_store = WeComKfContextStore(
                    path=Path(directory) / "context.json"
                )
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_context_store.save(
                    "kf_xxx:wm_xxx",
                    {
                        "recent_messages": [
                            {"role": "客户", "content": "皋塘还有房子吗？"},
                            {"role": "客服", "content": "皋塘运都16-1-1003还有。"},
                        ],
                        "updated_at": 9999999999.0,
                    },
                )

                search_text = main._kf_media_search_text(
                    "kf_xxx",
                    "wm_xxx",
                    "万达视频发我一下",
                )

                self.assertEqual(search_text, "万达视频发我一下")
            finally:
                main.wecom_kf_context_store = previous_store
                main.wecom_kf_conversation_memory.clear()

    def test_normalizes_xiaoyangba_typos_for_media_search(self) -> None:
        search_text = main._kf_media_search_text("kf_xxx", "wm_xxx", "小样吧视频发我一下")

        self.assertIn("小洋坝", search_text)
        self.assertNotIn("小样吧", search_text)

    def test_normalizes_wanqiu_aliases_for_media_search(self) -> None:
        self.assertEqual(
            main._normalize_media_search_aliases("晚秋视频发一下"),
            "琬秋铭府视频发一下",
        )
        self.assertEqual(
            main._normalize_media_search_aliases("婉秋视频发一下"),
            "琬秋铭府视频发一下",
        )
        self.assertEqual(
            main._normalize_media_search_aliases("琬秋铭府视频发一下"),
            "琬秋铭府视频发一下",
        )

    def test_plural_video_followup_uses_recent_two_room_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous_store = main.wecom_kf_context_store
            try:
                main.wecom_kf_context_store = WeComKfContextStore(
                    path=Path(directory) / "context.json"
                )
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_context_store.save(
                    "kf_xxx:wm_xxx",
                    {
                        "recent_messages": [
                            {
                                "role": "客服",
                                "content": "小区：永佳新苑\n房号：2-703\n户型：一室一厅",
                            },
                            {
                                "role": "客服",
                                "content": "小区：华丰人家\n房号：8-603\n户型：整租一室一厅",
                            },
                            {"role": "客户", "content": "这两套视频发一下"},
                        ],
                        "updated_at": 9999999999.0,
                    },
                )

                search_texts = main._kf_media_search_texts(
                    "kf_xxx",
                    "wm_xxx",
                    "这两套视频发一下",
                )

                self.assertEqual(len(search_texts), 2)
                self.assertIn("永佳新苑", search_texts[0])
                self.assertIn("2-703", search_texts[0])
                self.assertIn("华丰人家", search_texts[1])
                self.assertIn("8-603", search_texts[1])
            finally:
                main.wecom_kf_context_store = previous_store
                main.wecom_kf_conversation_memory.clear()


class WeComKfMessageTests(unittest.TestCase):
    def test_filters_customer_text_messages(self) -> None:
        message = {
            "msgid": "msg-1",
            "open_kfid": "kf_xxx",
            "external_userid": "wm_xxx",
            "origin": 3,
            "msgtype": "text",
            "text": {"content": "还有一室一厅吗"},
        }

        self.assertEqual(extract_kf_text(message), "还有一室一厅吗")
        self.assertTrue(should_auto_reply_kf_message(message))

    def test_rejects_non_customer_or_empty_messages(self) -> None:
        self.assertFalse(
            should_auto_reply_kf_message(
                {
                    "open_kfid": "kf_xxx",
                    "external_userid": "wm_xxx",
                    "origin": 4,
                    "msgtype": "text",
                    "text": {"content": "内部回复"},
                }
            )
        )
        self.assertFalse(
            should_auto_reply_kf_message(
                {
                    "open_kfid": "kf_xxx",
                    "external_userid": "wm_xxx",
                    "origin": 3,
                    "msgtype": "image",
                }
            )
        )

    def test_detects_kf_message_event(self) -> None:
        self.assertTrue(
            is_kf_message_event(
                {"Event": "kf_msg_or_event", "Token": "callback-token"}
            )
        )
        self.assertFalse(is_kf_message_event({"Event": "change_external_contact"}))

    def test_builds_send_text_payload(self) -> None:
        client = WeComKfClient()
        self.assertEqual(
            client.build_text_payload("kf_xxx", "wm_xxx", "你好"),
            {
                "touser": "wm_xxx",
                "open_kfid": "kf_xxx",
                "msgtype": "text",
                "text": {"content": "你好"},
            },
        )

    def test_builds_send_image_payload(self) -> None:
        client = WeComKfClient()
        self.assertEqual(
            client.build_image_payload("kf_xxx", "wm_xxx", "media_xxx"),
            {
                "touser": "wm_xxx",
                "open_kfid": "kf_xxx",
                "msgtype": "image",
                "image": {"media_id": "media_xxx"},
            },
        )

    def test_builds_send_video_payload(self) -> None:
        client = WeComKfClient()
        self.assertEqual(
            client.build_video_payload("kf_xxx", "wm_xxx", "media_xxx"),
            {
                "touser": "wm_xxx",
                "open_kfid": "kf_xxx",
                "msgtype": "video",
                "video": {"media_id": "media_xxx"},
            },
        )


class WeComKfVideoHelperTests(unittest.TestCase):
    def test_detects_video_requests(self) -> None:
        self.assertTrue(main._wants_video("小洋坝视频发一下"))
        self.assertTrue(main._wants_video("这套有实拍吗"))
        self.assertTrue(main._wants_video("华丰人家603笔记发一下"))
        self.assertFalse(main._wants_video("小洋坝还有房子吗"))

    def test_detects_inventory_image_requests(self) -> None:
        self.assertTrue(main._wants_inventory_image("我要的是房源表"))
        self.assertTrue(main._wants_inventory_image("一张图片"))
        self.assertTrue(main._wants_inventory_image("表发一下"))
        self.assertTrue(main._wants_inventory_image("发一下表"))
        self.assertTrue(main._wants_inventory_image("给我最新表"))
        self.assertFalse(main._wants_inventory_image("小洋坝视频发一下"))
        self.assertFalse(main._wants_inventory_image("我只是发表一下意见"))
        self.assertFalse(main._wants_inventory_image("电表怎么看"))

    def test_detects_satisfied_feedback_without_questions(self) -> None:
        self.assertTrue(main._is_satisfied_feedback("这个视频还可以"))
        self.assertTrue(main._is_satisfied_feedback("满意"))
        self.assertFalse(main._is_satisfied_feedback("不满意"))
        self.assertFalse(main._is_satisfied_feedback("能短租吗？"))
        self.assertFalse(main._is_satisfied_feedback("你好"))

    def test_detects_greeting_only(self) -> None:
        self.assertTrue(main._is_greeting_only("你好"))
        self.assertTrue(main._is_greeting_only("您好呀～"))
        self.assertFalse(main._is_greeting_only("你好，永佳还有房吗"))

    def test_polishes_note_wording_and_unavailable_viewing_contact(self) -> None:
        reply = main._polish_kf_reply_text(
            "永佳这套现在能看房吗，帮我联系一下",
            "永佳新苑2-703现在暂时看不了，6月2号才空出。需要我再发下详细笔记吗？",
        )

        self.assertIn("房间详细信息", reply)
        self.assertNotIn("笔记", reply)
        self.assertIn("18758141785", reply)
        self.assertIn("13282125992", reply)
        self.assertIn("19941091943", reply)
        self.assertNotIn("？。", reply)
        self.assertIn("房间详细信息吗？\n\n这类还没空出的房子", reply)

    def test_polishes_missing_video_claim_when_material_exists(self) -> None:
        reply = main._polish_kf_reply_text(
            "琬秋",
            "琬秋铭府3-702B目前没视频，看房要提前联系。你是想约时间看这套吗？",
            has_available_video=True,
        )

        self.assertIn("我这边有对应视频", reply)
        self.assertNotIn("没视频", reply)

    def test_detects_video_context_correction(self) -> None:
        self.assertTrue(
            main._is_dissatisfied_or_correction(
                "你这个视频存放的文件夹名字不就是小区和房间号吗，"
                "你怎么还问我要看哪个小区？"
            )
        )

    def test_does_not_treat_availability_question_as_dissatisfaction(self) -> None:
        self.assertFalse(main._is_dissatisfied_or_correction("万达还有没有带阳台的房间"))
        self.assertFalse(main._is_dissatisfied_or_correction("这个可以短租吗"))
        self.assertTrue(main._is_dissatisfied_or_correction("不满意，没有直接把视频发给我"))

    def test_builds_room_database_public_video_urls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous_root = main.settings.room_database_path
            previous_base_url = main.settings.public_base_url
            try:
                main.settings.room_database_path = Path(directory)
                main.settings.public_base_url = "https://example.com/"
                video_path = Path(directory) / "video" / "小洋坝二区6-901-4" / "微信视频.mp4"
                video_path.parent.mkdir(parents=True)
                video_path.write_bytes(b"video")

                self.assertEqual(
                    main._room_database_public_urls([video_path]),
                    [
                        "https://example.com/room-database/video/"
                        "%E5%B0%8F%E6%B4%8B%E5%9D%9D%E4%BA%8C%E5%8C%BA6-901-4/"
                        "%E5%BE%AE%E4%BF%A1%E8%A7%86%E9%A2%91.mp4"
                    ],
                )
            finally:
                main.settings.room_database_path = previous_root
                main.settings.public_base_url = previous_base_url


class WeComKfEventHandlingTests(unittest.IsolatedAsyncioTestCase):
    async def test_saves_cursor_after_successful_processing(self) -> None:
        class FakeStateStore:
            def __init__(self) -> None:
                self.processed: list[str] = []
                self.cursor = ""

            def mark_processed(self, msgid: str) -> None:
                self.processed.append(msgid)

            def save_cursor(self, cursor: str) -> None:
                self.cursor = cursor

        class FakeKfClient:
            def __init__(self) -> None:
                self.state_store = FakeStateStore()
                self.last_next_cursor = "cursor-next"

            async def sync_messages(self, open_kfid: str, token: str) -> list[dict]:
                return [
                    {
                        "msgid": "msg-1",
                        "open_kfid": open_kfid,
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "表发一下"},
                    }
                ]

        previous_client = main.wecom_kf
        previous_handler = main.handle_kf_message
        fake_client = FakeKfClient()
        handled: list[str] = []

        async def fake_handler(message: dict) -> None:
            handled.append(message["msgid"])

        try:
            main.wecom_kf = fake_client
            main.handle_kf_message = fake_handler
            await main.handle_kf_event({"OpenKfId": "kf_xxx", "Token": "token"})

            self.assertEqual(handled, ["msg-1"])
            self.assertEqual(fake_client.state_store.processed, ["msg-1"])
            self.assertEqual(fake_client.state_store.cursor, "cursor-next")
        finally:
            main.wecom_kf = previous_client
            main.handle_kf_message = previous_handler

    async def test_keeps_cursor_when_processing_fails(self) -> None:
        class FakeStateStore:
            def __init__(self) -> None:
                self.processed: list[str] = []
                self.cursor = ""

            def mark_processed(self, msgid: str) -> None:
                self.processed.append(msgid)

            def save_cursor(self, cursor: str) -> None:
                self.cursor = cursor

        class FakeKfClient:
            def __init__(self) -> None:
                self.state_store = FakeStateStore()
                self.last_next_cursor = "cursor-next"

            async def sync_messages(self, open_kfid: str, token: str) -> list[dict]:
                return [
                    {
                        "msgid": "msg-1",
                        "open_kfid": open_kfid,
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "表发一下"},
                    }
                ]

        previous_client = main.wecom_kf
        previous_handler = main.handle_kf_message
        fake_client = FakeKfClient()

        async def failing_handler(message: dict) -> None:
            raise RuntimeError("send failed")

        try:
            main.wecom_kf = fake_client
            main.handle_kf_message = failing_handler
            await main.handle_kf_event({"OpenKfId": "kf_xxx", "Token": "token"})

            self.assertEqual(fake_client.state_store.processed, [])
            self.assertEqual(fake_client.state_store.cursor, "")
        finally:
            main.wecom_kf = previous_client
            main.handle_kf_message = previous_handler

    async def test_serializes_concurrent_kf_callbacks(self) -> None:
        class FakeStateStore:
            def __init__(self) -> None:
                self.processed: list[str] = []
                self.cursor = ""

            def is_processed(self, msgid: str) -> bool:
                return msgid in self.processed

            def mark_processed(self, msgid: str) -> None:
                if msgid not in self.processed:
                    self.processed.append(msgid)

            def save_cursor(self, cursor: str) -> None:
                self.cursor = cursor

        class FakeKfClient:
            def __init__(self) -> None:
                self.state_store = FakeStateStore()
                self.last_next_cursor = "cursor-next"
                self.sync_calls = 0

            async def sync_messages(self, open_kfid: str, token: str) -> list[dict]:
                self.sync_calls += 1
                await asyncio.sleep(0.01)
                if self.state_store.is_processed("msg-1"):
                    return []
                return [
                    {
                        "msgid": "msg-1",
                        "open_kfid": open_kfid,
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "发我一下"},
                    }
                ]

        previous_client = main.wecom_kf
        previous_handler = main.handle_kf_message
        fake_client = FakeKfClient()
        handled: list[str] = []

        async def fake_handler(message: dict) -> None:
            handled.append(message["msgid"])

        try:
            main.wecom_kf = fake_client
            main.handle_kf_message = fake_handler
            await asyncio.gather(
                main.handle_kf_event({"OpenKfId": "kf_xxx", "Token": "token"}),
                main.handle_kf_event({"OpenKfId": "kf_xxx", "Token": "token"}),
            )

            self.assertEqual(handled, ["msg-1"])
            self.assertEqual(fake_client.state_store.processed, ["msg-1"])
            self.assertEqual(fake_client.sync_calls, 2)
        finally:
            main.wecom_kf = previous_client
            main.handle_kf_message = previous_handler


class WeComKfSatisfactionPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_greeting_gets_human_guidance_reply(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.texts: list[str] = []

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        previous_client = main.wecom_kf
        fake_client = FakeKfClient()
        try:
            main.wecom_kf = fake_client
            main.wecom_kf_idle_sequences.clear()

            await main.handle_kf_message(
                {
                    "msgid": "msg-1",
                    "open_kfid": "kf_xxx",
                    "external_userid": "wm_xxx",
                    "origin": 3,
                    "msgtype": "text",
                    "text": {"content": "你好"},
                }
            )

            self.assertEqual(len(fake_client.texts), 1)
            self.assertIn("我在的", fake_client.texts[0])
            self.assertIn("价格", fake_client.texts[0])
            self.assertIn("视频", fake_client.texts[0])
        finally:
            main.wecom_kf = previous_client
            main.wecom_kf_idle_sequences.clear()

    async def test_plain_text_reply_does_not_schedule_satisfaction_prompt(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.texts: list[str] = []

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        class FakeInventory:
            async def search(self, content: str) -> list[dict]:
                return []

            def format_rows(self, rows: list[dict]) -> str:
                return ""

            async def snapshot(self) -> str:
                return "暂无"

        class FakeMediaStore:
            def list_for_rooms(self, rooms: list[dict]) -> list:
                return []

            def public_urls(self, media: list) -> tuple[list[str], list[str]]:
                return [], []

            def list_room_database_videos(self, query: str, limit: int = 6) -> list[Path]:
                return []

        class FakeReplyGenerator:
            async def generate(
                self,
                message,
                inventory_snapshot: str,
                media_images: list[str],
                media_videos: list[str],
                conversation_context: str = "",
            ) -> ReplyPlan:
                return ReplyPlan(text="永佳新苑还有两套。")

        scheduled: list[tuple[str, str, int]] = []

        def fake_schedule(open_kfid: str, external_userid: str, sequence: int) -> None:
            scheduled.append((open_kfid, external_userid, sequence))

        previous_client = main.wecom_kf
        previous_inventory = main.inventory
        previous_media_store = main.media_store
        previous_reply_generator = main.reply_generator
        previous_schedule = main._schedule_kf_satisfaction_prompt
        try:
            main.wecom_kf = FakeKfClient()
            main.inventory = FakeInventory()
            main.media_store = FakeMediaStore()
            main.reply_generator = FakeReplyGenerator()
            main._schedule_kf_satisfaction_prompt = fake_schedule
            main.wecom_kf_idle_sequences.clear()

            await main.handle_kf_message(
                {
                    "msgid": "msg-1",
                    "open_kfid": "kf_xxx",
                    "external_userid": "wm_xxx",
                    "origin": 3,
                    "msgtype": "text",
                    "text": {"content": "永佳新苑房子还有吗"},
                }
            )

            self.assertEqual(scheduled, [])
        finally:
            main.wecom_kf = previous_client
            main.inventory = previous_inventory
            main.media_store = previous_media_store
            main.reply_generator = previous_reply_generator
            main._schedule_kf_satisfaction_prompt = previous_schedule
            main.wecom_kf_idle_sequences.clear()

    async def test_satisfaction_prompt_sender_is_disabled(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.sent: list[tuple[str, str, str]] = []

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.sent.append((open_kfid, external_userid, content))
                return {"errcode": 0}

        previous_client = main.wecom_kf
        previous_delay = main.settings.wecom_kf_satisfaction_delay_seconds
        fake_client = FakeKfClient()
        try:
            main.wecom_kf = fake_client
            main.settings.wecom_kf_satisfaction_delay_seconds = 0
            main.wecom_kf_idle_sequences.clear()
            sequence = main._next_kf_idle_sequence("kf_xxx", "wm_xxx")

            await main._send_kf_satisfaction_prompt_after_idle(
                "kf_xxx",
                "wm_xxx",
                sequence,
            )

            self.assertEqual(fake_client.sent, [])
        finally:
            main.wecom_kf = previous_client
            main.settings.wecom_kf_satisfaction_delay_seconds = previous_delay
            main.wecom_kf_idle_sequences.clear()

    async def test_skips_stale_satisfaction_prompt(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.sent: list[tuple[str, str, str]] = []

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.sent.append((open_kfid, external_userid, content))
                return {"errcode": 0}

        previous_client = main.wecom_kf
        previous_delay = main.settings.wecom_kf_satisfaction_delay_seconds
        fake_client = FakeKfClient()
        try:
            main.wecom_kf = fake_client
            main.settings.wecom_kf_satisfaction_delay_seconds = 0
            main.wecom_kf_idle_sequences.clear()
            stale_sequence = main._next_kf_idle_sequence("kf_xxx", "wm_xxx")
            main._next_kf_idle_sequence("kf_xxx", "wm_xxx")

            await main._send_kf_satisfaction_prompt_after_idle(
                "kf_xxx",
                "wm_xxx",
                stale_sequence,
            )

            self.assertEqual(fake_client.sent, [])
        finally:
            main.wecom_kf = previous_client
            main.settings.wecom_kf_satisfaction_delay_seconds = previous_delay
            main.wecom_kf_idle_sequences.clear()


class WeComKfContextMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_generator_receives_recent_dialog_context(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.texts: list[str] = []

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        class FakeInventory:
            async def search(self, content: str) -> list[dict]:
                return []

            def format_rows(self, rows: list[dict]) -> str:
                return ""

            async def snapshot(self) -> str:
                return "暂无"

        class FakeMediaStore:
            def list_for_rooms(self, rooms: list[dict]) -> list:
                return []

            def public_urls(self, media: list) -> tuple[list[str], list[str]]:
                return [], []

            def list_room_database_videos(self, query: str, limit: int = 6) -> list[Path]:
                return []

        class FakeReplyGenerator:
            def __init__(self) -> None:
                self.contexts: list[str] = []

            async def generate(
                self,
                message,
                inventory_snapshot: str,
                media_images: list[str],
                media_videos: list[str],
                conversation_context: str = "",
            ) -> ReplyPlan:
                self.contexts.append(conversation_context)
                return ReplyPlan(text=f"回复:{message.content}")

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_store = main.wecom_kf_context_store
            previous_inventory = main.inventory
            previous_media_store = main.media_store
            previous_reply_generator = main.reply_generator
            previous_delay = main.settings.wecom_kf_satisfaction_delay_seconds
            fake_client = FakeKfClient()
            fake_reply_generator = FakeReplyGenerator()
            try:
                main.wecom_kf = fake_client
                main.wecom_kf_context_store = WeComKfContextStore(
                    path=Path(directory) / "context.json"
                )
                main.inventory = FakeInventory()
                main.media_store = FakeMediaStore()
                main.reply_generator = fake_reply_generator
                main.settings.wecom_kf_satisfaction_delay_seconds = 999
                main.wecom_kf_conversation_memory.clear()

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "星桥有房子吗"},
                    }
                )
                await main.handle_kf_message(
                    {
                        "msgid": "msg-2",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "视频发我一下"},
                    }
                )

                self.assertEqual(len(fake_reply_generator.contexts), 2)
                latest_context = fake_reply_generator.contexts[-1]
                self.assertIn("星桥有房子吗", latest_context)
                self.assertIn("回复:星桥有房子吗", latest_context)
                self.assertIn("视频发我一下", latest_context)
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.inventory = previous_inventory
                main.media_store = previous_media_store
                main.reply_generator = previous_reply_generator
                main.settings.wecom_kf_satisfaction_delay_seconds = previous_delay
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()

    async def test_dissatisfied_followup_reuses_last_video_urls(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.texts: list[str] = []

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_store = main.wecom_kf_context_store
            previous_delay = main.settings.wecom_kf_satisfaction_delay_seconds
            fake_client = FakeKfClient()
            try:
                main.wecom_kf = fake_client
                main.wecom_kf_context_store = WeComKfContextStore(
                    path=Path(directory) / "context.json"
                )
                main.settings.wecom_kf_satisfaction_delay_seconds = 999
                main.wecom_kf_conversation_memory.clear()
                main._remember_kf_media_context(
                    "kf_xxx",
                    "wm_xxx",
                    video_urls=["https://example.com/video-1.mp4", "https://example.com/video-2.mp4"],
                )

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "你明明有啊"},
                    }
                )

                self.assertEqual(len(fake_client.texts), 1)
                self.assertIn("video-1.mp4", fake_client.texts[0])
                self.assertIn("video-2.mp4", fake_client.texts[0])
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.settings.wecom_kf_satisfaction_delay_seconds = previous_delay
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()

    async def test_correction_followup_reuses_persisted_video_paths(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.videos: list[Path] = []
                self.texts: list[str] = []

            async def send_video(
                self,
                open_kfid: str,
                external_userid: str,
                video_path: Path,
            ) -> dict:
                self.videos.append(video_path)
                return {"errcode": 0}

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_store = main.wecom_kf_context_store
            previous_delay = main.settings.wecom_kf_satisfaction_delay_seconds
            fake_client = FakeKfClient()
            video_path = Path("room_database/video/小洋坝三区12-1003-2/微信视频.mp4")
            try:
                main.wecom_kf = fake_client
                main.wecom_kf_context_store = WeComKfContextStore(
                    path=Path(directory) / "context.json"
                )
                main.settings.wecom_kf_satisfaction_delay_seconds = 999
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_context_store.save(
                    "kf_xxx:wm_xxx",
                    {
                        "video_paths": [video_path],
                        "updated_at": 9999999999.0,
                    },
                )

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {
                            "content": "你这个视频存放的文件夹名字不就是小区和房间号吗，你怎么还问？"
                        },
                    }
                )

                self.assertEqual(fake_client.videos, [video_path])
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.settings.wecom_kf_satisfaction_delay_seconds = previous_delay
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()


class WeComKfSatisfiedFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_satisfied_feedback_gets_short_ack(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.texts: list[str] = []

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        previous_client = main.wecom_kf
        fake_client = FakeKfClient()
        try:
            main.wecom_kf = fake_client
            await main.handle_kf_message(
                {
                    "msgid": "msg-1",
                    "open_kfid": "kf_xxx",
                    "external_userid": "wm_xxx",
                    "origin": 3,
                    "msgtype": "text",
                    "text": {"content": "这个视频还可以"},
                }
            )

            self.assertEqual(fake_client.texts, ["好的，有需要随时发我。"])
        finally:
            main.wecom_kf = previous_client
            main.wecom_kf_idle_sequences.clear()


class WeComKfContractContactTests(unittest.IsolatedAsyncioTestCase):
    async def test_contract_question_returns_table_contact_numbers(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.texts: list[str] = []

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        previous_client = main.wecom_kf
        previous_schedule = main._schedule_kf_satisfaction_prompt
        scheduled: list[tuple[str, str, int]] = []

        def fake_schedule(open_kfid: str, external_userid: str, sequence: int) -> None:
            scheduled.append((open_kfid, external_userid, sequence))

        try:
            main.wecom_kf = FakeKfClient()
            main._schedule_kf_satisfaction_prompt = fake_schedule
            main.wecom_kf_idle_sequences.clear()

            await main.handle_kf_message(
                {
                    "msgid": "msg-1",
                    "open_kfid": "kf_xxx",
                    "external_userid": "wm_xxx",
                    "origin": 3,
                    "msgtype": "text",
                    "text": {"content": "我联系谁签合同呢？"},
                }
            )

            self.assertEqual(len(main.wecom_kf.texts), 1)
            self.assertIn("18758141785", main.wecom_kf.texts[0])
            self.assertIn("13282125992", main.wecom_kf.texts[0])
            self.assertIn("19941091943", main.wecom_kf.texts[0])
            self.assertNotIn("联系我", main.wecom_kf.texts[0])
            self.assertEqual(scheduled, [])
        finally:
            main.wecom_kf = previous_client
            main._schedule_kf_satisfaction_prompt = previous_schedule
            main.wecom_kf_idle_sequences.clear()

    def test_detects_booking_questions(self) -> None:
        self.assertTrue(main._wants_contract_contact("已经看完房子了。怎么订房"))
        self.assertTrue(main._wants_contract_contact("我联系谁签合同呢？"))


class WeComKfDepositWaiverTests(unittest.IsolatedAsyncioTestCase):
    async def test_deposit_waiver_question_returns_credit_rules(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.texts: list[str] = []

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        previous_client = main.wecom_kf
        previous_schedule = main._schedule_kf_satisfaction_prompt
        scheduled: list[tuple[str, str, int]] = []

        def fake_schedule(open_kfid: str, external_userid: str, sequence: int) -> None:
            scheduled.append((open_kfid, external_userid, sequence))

        try:
            main.wecom_kf = FakeKfClient()
            main._schedule_kf_satisfaction_prompt = fake_schedule
            main.wecom_kf_idle_sequences.clear()

            await main.handle_kf_message(
                {
                    "msgid": "msg-1",
                    "open_kfid": "kf_xxx",
                    "external_userid": "wm_xxx",
                    "origin": 3,
                    "msgtype": "text",
                    "text": {"content": "免押金的条件"},
                }
            )

            self.assertEqual(len(main.wecom_kf.texts), 1)
            reply = main.wecom_kf.texts[0]
            self.assertIn("支付宝芝麻信用", reply)
            self.assertIn("无忧住", reply)
            self.assertIn("550", reply)
            self.assertIn("3-12", reply)
            self.assertIn("电子合同", reply)
            self.assertIn("5.5%-8%", reply)
            self.assertIn("建行惠市宝", reply)
            self.assertIn("仅新签合同支持免押", reply)
            self.assertNotIn("完全免费免押", reply)
            self.assertEqual(scheduled, [])
        finally:
            main.wecom_kf = previous_client
            main._schedule_kf_satisfaction_prompt = previous_schedule
            main.wecom_kf_idle_sequences.clear()

    async def test_can_do_deposit_waiver_returns_short_eligibility_check(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.texts: list[str] = []

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        previous_client = main.wecom_kf
        previous_schedule = main._schedule_kf_satisfaction_prompt

        def fake_schedule(open_kfid: str, external_userid: str, sequence: int) -> None:
            return None

        try:
            main.wecom_kf = FakeKfClient()
            main._schedule_kf_satisfaction_prompt = fake_schedule
            main.wecom_kf_idle_sequences.clear()

            await main.handle_kf_message(
                {
                    "msgid": "msg-1",
                    "open_kfid": "kf_xxx",
                    "external_userid": "wm_xxx",
                    "origin": 3,
                    "msgtype": "text",
                    "text": {"content": "能做免押吗"},
                }
            )

            reply = main.wecom_kf.texts[0]
            self.assertIn("芝麻分大于等于 550", reply)
            self.assertIn("自查方式", reply)
            self.assertIn("租房板块申请额度", reply)
            self.assertNotIn("免押服务费：", reply)
            self.assertNotIn("5.5%-8%", reply)
        finally:
            main.wecom_kf = previous_client
            main._schedule_kf_satisfaction_prompt = previous_schedule
            main.wecom_kf_idle_sequences.clear()

    async def test_deposit_waiver_fee_question_returns_fee_only(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.texts: list[str] = []

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        previous_client = main.wecom_kf
        previous_schedule = main._schedule_kf_satisfaction_prompt

        def fake_schedule(open_kfid: str, external_userid: str, sequence: int) -> None:
            return None

        try:
            main.wecom_kf = FakeKfClient()
            main._schedule_kf_satisfaction_prompt = fake_schedule
            main.wecom_kf_idle_sequences.clear()

            await main.handle_kf_message(
                {
                    "msgid": "msg-1",
                    "open_kfid": "kf_xxx",
                    "external_userid": "wm_xxx",
                    "origin": 3,
                    "msgtype": "text",
                    "text": {"content": "免押服务费多少"},
                }
            )

            reply = main.wecom_kf.texts[0]
            self.assertIn("5.5%", reply)
            self.assertIn("7%", reply)
            self.assertIn("8%", reply)
            self.assertNotIn("自查方式", reply)
        finally:
            main.wecom_kf = previous_client
            main._schedule_kf_satisfaction_prompt = previous_schedule
            main.wecom_kf_idle_sequences.clear()

    def test_detects_deposit_waiver_questions(self) -> None:
        self.assertTrue(main._wants_deposit_waiver("免押金怎么办"))
        self.assertTrue(main._wants_deposit_waiver("无忧住怎么申请"))
        self.assertTrue(main._wants_deposit_waiver("芝麻信用多少分可以免押"))
        self.assertFalse(main._wants_deposit_waiver("押一付一多少钱"))


class WeComKfRoomDatabaseVideoSendTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_send_followup_sends_latest_room_video(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.videos: list[Path] = []
                self.texts: list[str] = []

            async def send_video(
                self,
                open_kfid: str,
                external_userid: str,
                video_path: Path,
            ) -> dict:
                self.videos.append(video_path)
                return {"errcode": 0}

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        class FakeInventory:
            async def search(self, content: str) -> list[dict]:
                return []

            def format_rows(self, rows: list[dict]) -> str:
                return ""

            async def snapshot(self) -> str:
                return "暂无"

        class FakeMediaStore:
            def __init__(self, video_path: Path) -> None:
                self.video_path = video_path
                self.queries: list[str] = []
                self.limits: list[int] = []

            def list_for_rooms(self, rooms: list[dict]) -> list:
                return []

            def public_urls(self, media: list) -> tuple[list[str], list[str]]:
                return [], []

            def list_room_database_videos(self, query: str, limit: int = 6) -> list[Path]:
                self.queries.append(query)
                self.limits.append(limit)
                if "小洋坝家园" in query and "二区6-801-3" in query:
                    return [self.video_path]
                return []

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_store = main.wecom_kf_context_store
            previous_inventory = main.inventory
            previous_media_store = main.media_store
            previous_delay = main.settings.wecom_kf_satisfaction_delay_seconds
            fake_client = FakeKfClient()
            video_path = Path("room_database/video/小洋坝家园二区6-801C/video.mp4")
            fake_media_store = FakeMediaStore(video_path)
            try:
                main.wecom_kf = fake_client
                main.wecom_kf_context_store = WeComKfContextStore(
                    path=Path(directory) / "context.json"
                )
                main.inventory = FakeInventory()
                main.media_store = fake_media_store
                main.settings.wecom_kf_satisfaction_delay_seconds = 999
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_context_store.save(
                    "kf_xxx:wm_xxx",
                    {
                        "recent_messages": [
                            {
                                "role": "客服",
                                "content": (
                                    "小区：小洋坝家园\n"
                                    "房号：二区6-801-3\n"
                                    "户型：一室独立厨卫带阳台\n"
                                    "我把视频发你哈。"
                                ),
                            },
                        ],
                        "updated_at": 9999999999.0,
                    },
                )

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "发我"},
                    }
                )

                self.assertEqual(fake_client.texts, ["这是小洋坝家园二区6-801C的视频。"])
                self.assertEqual(fake_client.videos, [video_path])
                self.assertIn("小洋坝家园", fake_media_store.queries[0])
                self.assertIn("二区6-801-3", fake_media_store.queries[0])
                self.assertEqual(fake_media_store.limits, [1])
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.inventory = previous_inventory
                main.media_store = previous_media_store
                main.settings.wecom_kf_satisfaction_delay_seconds = previous_delay
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()

    async def test_plural_send_followup_sends_two_recent_room_videos(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.videos: list[Path] = []
                self.texts: list[str] = []

            async def send_video(
                self,
                open_kfid: str,
                external_userid: str,
                video_path: Path,
            ) -> dict:
                self.videos.append(video_path)
                return {"errcode": 0}

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        class FakeInventory:
            async def search(self, content: str) -> list[dict]:
                return []

            def format_rows(self, rows: list[dict]) -> str:
                return ""

            async def snapshot(self) -> str:
                return "暂无"

        class FakeMediaStore:
            def __init__(self) -> None:
                self.queries: list[str] = []
                self.limits: list[int] = []
                self.yongjia = Path("room_database/video/永佳新苑2-703/视频.mp4")
                self.huafeng = Path("room_database/video/华丰人家8-603/视频.mp4")

            def list_for_rooms(self, rooms: list[dict]) -> list:
                return []

            def public_urls(self, media: list) -> tuple[list[str], list[str]]:
                return [], []

            def list_room_database_videos(self, query: str, limit: int = 6) -> list[Path]:
                self.queries.append(query)
                self.limits.append(limit)
                if "永佳新苑" in query and "2-703" in query:
                    return [self.yongjia]
                if "华丰人家" in query and "8-603" in query:
                    return [self.huafeng]
                return []

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_store = main.wecom_kf_context_store
            previous_inventory = main.inventory
            previous_media_store = main.media_store
            fake_client = FakeKfClient()
            fake_media_store = FakeMediaStore()
            try:
                main.wecom_kf = fake_client
                main.wecom_kf_context_store = WeComKfContextStore(
                    path=Path(directory) / "context.json"
                )
                main.inventory = FakeInventory()
                main.media_store = fake_media_store
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_context_store.save(
                    "kf_xxx:wm_xxx",
                    {
                        "recent_messages": [
                            {
                                "role": "客服",
                                "content": "小区：永佳新苑\n房号：2-703\n户型：一室一厅",
                            },
                            {
                                "role": "客服",
                                "content": "小区：华丰人家\n房号：8-603\n户型：整租一室一厅",
                            },
                        ],
                        "updated_at": 9999999999.0,
                    },
                )

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "这两套视频发一下"},
                    }
                )

                self.assertEqual(
                    fake_client.texts,
                    ["这是永佳新苑2-703的视频。", "这是华丰人家8-603的视频。"],
                )
                self.assertEqual(fake_client.videos, [fake_media_store.yongjia, fake_media_store.huafeng])
                self.assertEqual(fake_media_store.limits, [1, 1])
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.inventory = previous_inventory
                main.media_store = previous_media_store
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()

    async def test_sends_room_database_videos_as_native_videos(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.videos: list[Path] = []
                self.texts: list[str] = []

            async def send_video(
                self,
                open_kfid: str,
                external_userid: str,
                video_path: Path,
            ) -> dict:
                self.videos.append(video_path)
                return {"errcode": 0}

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        previous_client = main.wecom_kf
        fake_client = FakeKfClient()
        video_path = Path("room_database/video/小洋坝/视频.mp4")
        try:
            main.wecom_kf = fake_client
            sent = await main._send_kf_room_database_videos(
                "kf_xxx",
                "wm_xxx",
                [video_path],
            )

            self.assertTrue(sent)
            self.assertEqual(fake_client.videos, [video_path])
            self.assertEqual(fake_client.texts, ["这是小洋坝的视频。"])
        finally:
            main.wecom_kf = previous_client

    async def test_transcodes_large_room_database_video_before_sending(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.videos: list[Path] = []
                self.texts: list[str] = []

            async def send_video(
                self,
                open_kfid: str,
                external_userid: str,
                video_path: Path,
            ) -> dict:
                self.videos.append(video_path)
                return {"errcode": 0}

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        previous_client = main.wecom_kf
        previous_needs = main.needs_wecom_video_transcode
        previous_prepare = main.prepare_wecom_video
        fake_client = FakeKfClient()
        original = Path("room_database/video/华丰人家8-603/original.mp4")
        compressed = Path("room_database/video/华丰人家8-603/.wecom_cache/original.wecom.mp4")
        try:
            main.wecom_kf = fake_client
            main.needs_wecom_video_transcode = lambda path: True
            main.prepare_wecom_video = lambda path, force=False: compressed

            sent = await main._send_kf_room_database_videos("kf_xxx", "wm_xxx", [original])

            self.assertTrue(sent)
            self.assertEqual(fake_client.videos, [compressed])
            self.assertEqual(fake_client.texts, ["这是华丰人家8-603的视频。"])
        finally:
            main.wecom_kf = previous_client
            main.needs_wecom_video_transcode = previous_needs
            main.prepare_wecom_video = previous_prepare

    async def test_retries_video_upload_with_transcoded_copy_after_upload_error(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.uploads: list[Path] = []
                self.media_ids: list[str] = []
                self.texts: list[str] = []

            async def upload_media(
                self,
                video_path: Path,
                media_type: str = "video",
            ) -> str:
                self.uploads.append(video_path)
                if len(self.uploads) == 1:
                    raise RuntimeError("invalid video size")
                return "media_xxx"

            async def send_video_media(
                self,
                open_kfid: str,
                external_userid: str,
                media_id: str,
            ) -> dict:
                self.media_ids.append(media_id)
                return {"errcode": 0}

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        previous_client = main.wecom_kf
        previous_needs = main.needs_wecom_video_transcode
        previous_prepare = main.prepare_wecom_video
        fake_client = FakeKfClient()
        original = Path("room_database/video/华丰人家8-603/original.mp4")
        compressed = Path("room_database/video/华丰人家8-603/.wecom_cache/original.wecom.mp4")
        try:
            main.wecom_kf = fake_client
            main.needs_wecom_video_transcode = lambda path: False
            main.prepare_wecom_video = lambda path, force=False: compressed

            sent = await main._send_kf_room_database_videos("kf_xxx", "wm_xxx", [original])

            self.assertTrue(sent)
            self.assertEqual(fake_client.uploads, [original, compressed])
            self.assertEqual(fake_client.media_ids, ["media_xxx"])
            self.assertEqual(fake_client.texts, ["这是华丰人家8-603的视频。"])
        finally:
            main.wecom_kf = previous_client
            main.needs_wecom_video_transcode = previous_needs
            main.prepare_wecom_video = previous_prepare

    async def test_does_not_send_room_label_when_video_upload_fails(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.texts: list[str] = []

            async def upload_media(self, video_path: Path, media_type: str = "video") -> str:
                raise RuntimeError("upload failed")

            async def send_video_media(
                self,
                open_kfid: str,
                external_userid: str,
                media_id: str,
            ) -> dict:
                return {"errcode": 0}

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_root = main.settings.room_database_path
            previous_base_url = main.settings.public_base_url
            previous_prepare = main.prepare_wecom_video
            fake_client = FakeKfClient()
            try:
                main.wecom_kf = fake_client
                main.settings.room_database_path = Path(directory)
                main.settings.public_base_url = "https://example.com"
                main.prepare_wecom_video = lambda path, force=False: (_ for _ in ()).throw(
                    RuntimeError("transcode failed")
                )
                video_path = Path(directory) / "video" / "琬秋铭府1-1803" / "视频.mp4"
                video_path.parent.mkdir(parents=True)
                video_path.write_bytes(b"not-a-real-video")

                sent = await main._send_kf_room_database_videos("kf_xxx", "wm_xxx", [video_path])

                self.assertTrue(sent)
                self.assertEqual(len(fake_client.texts), 1)
                self.assertIn("视频直发失败", fake_client.texts[0])
                self.assertNotIn("这是琬秋铭府1-1803的视频", fake_client.texts[0])
            finally:
                main.wecom_kf = previous_client
                main.settings.room_database_path = previous_root
                main.settings.public_base_url = previous_base_url
                main.prepare_wecom_video = previous_prepare


class WeComKfInventoryImageSendTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_inventory_images_as_native_images(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.images: list[Path] = []
                self.texts: list[str] = []

            async def send_image(
                self,
                open_kfid: str,
                external_userid: str,
                image_path: Path,
            ) -> dict:
                self.images.append(image_path)
                return {"errcode": 0}

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_root = main.settings.room_database_path
            previous_glob = main.settings.inventory_image_glob
            fake_client = FakeKfClient()
            image_path = Path(directory) / "inventory_1.png"
            image_path.write_bytes(b"image")
            try:
                main.wecom_kf = fake_client
                main.settings.room_database_path = Path(directory) / "room_database"
                main.settings.inventory_image_glob = str(image_path)

                sent = await main._send_kf_inventory_images(
                    "kf_xxx",
                    "wm_xxx",
                    [image_path],
                )

                self.assertTrue(sent)
                self.assertEqual(fake_client.images, [image_path])
                self.assertEqual(fake_client.texts, [])
            finally:
                main.wecom_kf = previous_client
                main.settings.room_database_path = previous_root
                main.settings.inventory_image_glob = previous_glob

    async def test_falls_back_to_links_when_native_image_send_fails(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.texts: list[str] = []

            async def send_image(
                self,
                open_kfid: str,
                external_userid: str,
                image_path: Path,
            ) -> dict:
                raise RuntimeError("upload failed")

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_root = main.settings.room_database_path
            previous_base_url = main.settings.public_base_url
            fake_client = FakeKfClient()
            root = Path(directory)
            image_path = root / "inventory_1.png"
            image_path.write_bytes(b"image")
            try:
                main.wecom_kf = fake_client
                main.settings.room_database_path = root
                main.settings.public_base_url = "https://example.com"

                sent = await main._send_kf_inventory_images(
                    "kf_xxx",
                    "wm_xxx",
                    [image_path],
                )

                self.assertTrue(sent)
                self.assertEqual(len(fake_client.texts), 2)
                self.assertIn("直发失败", fake_client.texts[0])
                self.assertEqual(
                    fake_client.texts[1],
                    "https://example.com/room-database/inventory_1.png",
                )
            finally:
                main.wecom_kf = previous_client
                main.settings.room_database_path = previous_root
                main.settings.public_base_url = previous_base_url

    async def test_stops_inventory_image_send_on_send_limit(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.texts: list[str] = []

            async def send_image(
                self,
                open_kfid: str,
                external_userid: str,
                image_path: Path,
            ) -> dict:
                raise WeComKfSendLimitError("send msg count limit")

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        previous_client = main.wecom_kf
        fake_client = FakeKfClient()
        try:
            main.wecom_kf = fake_client
            sent = await main._send_kf_inventory_images(
                "kf_xxx",
                "wm_xxx",
                [Path("inventory_1.png")],
            )

            self.assertFalse(sent)
            self.assertEqual(fake_client.texts, [])
        finally:
            main.wecom_kf = previous_client


if __name__ == "__main__":
    unittest.main()
