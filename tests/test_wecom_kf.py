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

    def test_persists_send_limited_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            store = WeComKfContextStore(path=path)

            store.save(
                "kf_xxx:wm_xxx",
                {
                    "send_limited": {
                        "triggered_at": 123.0,
                        "summary": "批量视频触发限流",
                        "video_urls": ["https://example.com/video-1.mp4"],
                    },
                    "updated_at": 124.0,
                },
            )

            context = store.get("kf_xxx:wm_xxx")

            self.assertEqual(
                context["send_limited"],
                {
                    "triggered_at": 123.0,
                    "summary": "批量视频触发限流",
                    "video_urls": ["https://example.com/video-1.mp4"],
                },
            )


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

    def test_polite_generic_video_followup_uses_latest_assistant_room_detail(self) -> None:
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
                                "content": (
                                    "杨家府（兴业杨家府）我这边看到还有两套：\n"
                                    "1. 房号10-1-304，一室一厅，押一付4500/押二付4200。\n"
                                    "2. 房号1-202，一室一厅，押一付4200/押一付3800。"
                                ),
                            },
                        ],
                        "updated_at": 9999999999.0,
                    },
                )

                search_text = main._kf_media_search_text(
                    "kf_xxx",
                    "wm_xxx",
                    "视频麻烦发我一下",
                )

                self.assertIn("杨家府", search_text)
                self.assertIn("10-1-304", search_text)
                self.assertIn("1-202", search_text)
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
        self.assertTrue(main._wants_video("小洋坝视屏发一下"))
        self.assertTrue(main._wants_video("这套有实拍吗"))
        self.assertTrue(main._wants_video("华丰人家603笔记发一下"))
        self.assertFalse(main._wants_video("小洋坝还有房子吗"))

    def test_detects_inventory_image_requests(self) -> None:
        self.assertTrue(main._wants_inventory_image("我要的是房源表"))
        self.assertFalse(main._wants_inventory_image("一张图片"))
        self.assertFalse(main._wants_inventory_image("照片发一下"))
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

    async def test_skips_send_limit_message_and_saves_cursor(self) -> None:
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
                        "text": {"content": "石桥铭苑6-1102"},
                    }
                ]

        previous_client = main.wecom_kf
        previous_handler = main.handle_kf_message
        previous_store = main.wecom_kf_context_store
        fake_client = FakeKfClient()

        async def send_limited_handler(message: dict) -> None:
            raise WeComKfSendLimitError("send msg count limit")

        try:
            with tempfile.TemporaryDirectory() as directory:
                main.wecom_kf = fake_client
                main.wecom_kf_context_store = WeComKfContextStore(path=Path(directory) / "context.json")
                main.wecom_kf_conversation_memory.clear()
                main.handle_kf_message = send_limited_handler
                await main.handle_kf_event({"OpenKfId": "kf_xxx", "Token": "token"})

                self.assertEqual(fake_client.state_store.processed, ["msg-1"])
                self.assertEqual(fake_client.state_store.cursor, "cursor-next")
                context = main.wecom_kf_context_store.get("kf_xxx:wm_xxx")
                self.assertIn("send_limited", context)
                self.assertIn("石桥铭苑6-1102", context["send_limited"]["summary"])
        finally:
            main.wecom_kf = previous_client
            main.wecom_kf_context_store = previous_store
            main.wecom_kf_conversation_memory.clear()
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
            async def search(self, content: str, limit: int = 8) -> list[dict]:
                return [{"小区": "小洋坝家园", "房号": "二区6-801-3"}]

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

    async def test_final_validation_blocks_stale_available_room_from_context(self) -> None:
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
            async def search(self, content: str, limit: int = 8) -> list[dict]:
                if not content.strip():
                    return [
                        {"小区": "棠润府", "房号": "1-602A"},
                        {"小区": "长木府", "房号": "3-1002B"},
                    ][:limit]
                return []

            def format_rows(self, rows: list[dict]) -> str:
                return ""

            async def snapshot(self) -> str:
                return "棠润府1-602A；长木府3-1002B"

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
                return ReplyPlan(text="星桥目前就锦绣嘉苑这套21-1801-2在租，押一付1880。")

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_store = main.wecom_kf_context_store
            previous_inventory = main.inventory
            previous_media_store = main.media_store
            previous_reply_generator = main.reply_generator
            previous_key = main.settings.dashscope_api_key
            fake_client = FakeKfClient()
            try:
                main.wecom_kf = fake_client
                main.wecom_kf_context_store = WeComKfContextStore(
                    path=Path(directory) / "context.json"
                )
                main.inventory = FakeInventory()
                main.media_store = FakeMediaStore()
                main.reply_generator = FakeReplyGenerator()
                main.settings.dashscope_api_key = ""
                main.wecom_kf_conversation_memory.clear()

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "星桥还有房子吗"},
                    }
                )

                self.assertEqual(len(fake_client.texts), 1)
                self.assertIn("最新房源表里查不到", fake_client.texts[0])
                self.assertNotIn("21-1801-2在租", fake_client.texts[0])
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.inventory = previous_inventory
                main.media_store = previous_media_store
                main.reply_generator = previous_reply_generator
                main.settings.dashscope_api_key = previous_key
                main.wecom_kf_conversation_memory.clear()
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
            async def search(self, content: str, limit: int = 8) -> list[dict]:
                if "华丰人家" in content:
                    return [{"小区": "华丰人家", "房号": "8-603"}]
                return [
                    {"小区": "永佳新苑", "房号": "2-703"},
                ]

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
                        "text": {"content": "那星桥价格多少"},
                    }
                )

                self.assertEqual(len(fake_reply_generator.contexts), 2)
                latest_context = fake_reply_generator.contexts[-1]
                self.assertIn("星桥有房子吗", latest_context)
                self.assertIn("回复:星桥有房子吗", latest_context)
                self.assertIn("那星桥价格多少", latest_context)
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

    async def test_original_video_followup_sends_direct_link(self) -> None:
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
            fake_client = FakeKfClient()
            try:
                main.wecom_kf = fake_client
                main.wecom_kf_context_store = WeComKfContextStore(
                    path=Path(directory) / "context.json"
                )
                main.wecom_kf_conversation_memory.clear()
                main._remember_kf_media_context(
                    "kf_xxx",
                    "wm_xxx",
                    video_urls=["https://example.com/original.mp4"],
                )

                for content in ("有没有更清楚的原视频", "有没有清楚一点的"):
                    await main.handle_kf_message(
                        {
                            "msgid": f"msg-{len(fake_client.texts) + 1}",
                            "open_kfid": "kf_xxx",
                            "external_userid": "wm_xxx",
                            "origin": 3,
                            "msgtype": "text",
                            "text": {"content": content},
                        }
                    )

                self.assertEqual(len(fake_client.texts), 2)
                self.assertTrue(all("原视频直达链接" in text for text in fake_client.texts))
                self.assertTrue(
                    all("https://example.com/original.mp4" in text for text in fake_client.texts)
                )
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()

    async def test_send_followup_after_original_video_prompt_sends_direct_link(self) -> None:
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
            fake_client = FakeKfClient()
            try:
                main.wecom_kf = fake_client
                main.wecom_kf_context_store = WeComKfContextStore(
                    path=Path(directory) / "context.json"
                )
                main.wecom_kf_conversation_memory.clear()
                main._remember_kf_media_context(
                    "kf_xxx",
                    "wm_xxx",
                    video_urls=["https://example.com/original.mp4"],
                )

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "发我吧"},
                    }
                )

                self.assertEqual(len(fake_client.texts), 1)
                self.assertIn("https://example.com/original.mp4", fake_client.texts[0])
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()

    async def test_viewing_followup_after_video_uses_recent_room_context(self) -> None:
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
            fake_client = FakeKfClient()
            video_path = Path("room_database/video/房源素材/诸葛龙吟院13-1101/视频.mp4")
            try:
                main.wecom_kf = fake_client
                main.wecom_kf_context_store = WeComKfContextStore(
                    path=Path(directory) / "context.json"
                )
                main.wecom_kf_conversation_memory.clear()
                main._remember_kf_media_context(
                    "kf_xxx",
                    "wm_xxx",
                    video_paths=[video_path],
                    video_urls=["https://example.com/video.mp4"],
                )

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "怎么看房"},
                    }
                )

                self.assertEqual(len(fake_client.texts), 1)
                self.assertIn("诸葛龙吟院13-1101", fake_client.texts[0])
                self.assertIn("18758141785", fake_client.texts[0])
                self.assertIn("13282125992", fake_client.texts[0])
                self.assertIn("19941091943", fake_client.texts[0])
                self.assertNotIn("你把小区或房号", fake_client.texts[0])
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()

    async def test_viewing_followup_after_video_replies_room_password_first(self) -> None:
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
            async def search(self, query: str, limit: int = 8) -> list[dict]:
                self.query = query
                return [
                    {
                        "小区": "诸葛龙吟院",
                        "房号": "13-1101",
                        "密码": "336699#",
                    }
                ]

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_store = main.wecom_kf_context_store
            previous_inventory = main.inventory
            fake_client = FakeKfClient()
            fake_inventory = FakeInventory()
            video_path = Path("room_database/video/房源素材/诸葛龙吟院13-1101/视频.mp4")
            try:
                main.wecom_kf = fake_client
                main.inventory = fake_inventory
                main.wecom_kf_context_store = WeComKfContextStore(
                    path=Path(directory) / "context.json"
                )
                main.wecom_kf_conversation_memory.clear()
                main._remember_kf_media_context(
                    "kf_xxx",
                    "wm_xxx",
                    video_paths=[video_path],
                    video_urls=["https://example.com/video.mp4"],
                )

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "怎么看房"},
                    }
                )

                self.assertEqual(len(fake_client.texts), 1)
                self.assertIn("诸葛龙吟院13-1101看房密码是：336699#", fake_client.texts[0])
                self.assertIn("如果现场密码不对", fake_client.texts[0])
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.inventory = previous_inventory
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()

    async def test_explicit_viewing_query_without_video_context_queries_password(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.texts: list[str] = []

            async def send_text(self, open_kfid: str, external_userid: str, content: str) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        class FakeInventory:
            async def search(self, query: str, limit: int = 8) -> list[dict]:
                return [{"小区": "长木府", "房号": "3-1002B", "密码": "123456#"}]

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_store = main.wecom_kf_context_store
            previous_inventory = main.inventory
            try:
                main.wecom_kf = FakeKfClient()
                main.inventory = FakeInventory()
                main.wecom_kf_context_store = WeComKfContextStore(path=Path(directory) / "context.json")
                main.wecom_kf_conversation_memory.clear()

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "长木府3-1002B怎么看房"},
                    }
                )

                self.assertIn("长木府3-1002B看房密码是：123456#", main.wecom_kf.texts[0])
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.inventory = previous_inventory
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()

    async def test_generic_viewing_query_without_room_asks_for_room_before_llm(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.texts: list[str] = []

            async def send_text(self, open_kfid: str, external_userid: str, content: str) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_store = main.wecom_kf_context_store
            try:
                main.wecom_kf = FakeKfClient()
                main.wecom_kf_context_store = WeComKfContextStore(path=Path(directory) / "context.json")
                main.wecom_kf_conversation_memory.clear()

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "怎么看房"},
                    }
                )

                self.assertEqual(len(main.wecom_kf.texts), 1)
                self.assertIn("小区和房号", main.wecom_kf.texts[0])
                self.assertIn("先查房源表里的看房密码", main.wecom_kf.texts[0])
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()

    async def test_wrong_password_followup_sends_contact_numbers(self) -> None:
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
            fake_client = FakeKfClient()
            video_path = Path("room_database/video/房源素材/诸葛龙吟院13-1101/视频.mp4")
            try:
                main.wecom_kf = fake_client
                main.wecom_kf_context_store = WeComKfContextStore(
                    path=Path(directory) / "context.json"
                )
                main.wecom_kf_conversation_memory.clear()
                main._remember_kf_media_context(
                    "kf_xxx",
                    "wm_xxx",
                    video_paths=[video_path],
                    video_urls=["https://example.com/video.mp4"],
                )

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "密码不对，开不了门"},
                    }
                )

                self.assertEqual(len(fake_client.texts), 1)
                self.assertIn("看房密码建议直接联系", fake_client.texts[0])
                self.assertIn("18758141785", fake_client.texts[0])
                self.assertIn("13282125992", fake_client.texts[0])
                self.assertIn("19941091943", fake_client.texts[0])
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()

    async def test_explicit_viewing_query_overrides_recent_video_context(self) -> None:
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
            async def search(self, query: str, limit: int = 8) -> list[dict]:
                if "香柠颜家府" in query or "2-2-1401B" in query:
                    return [
                        {
                            "小区": "香柠颜家府",
                            "房号": "2-2-1401B",
                            "密码": "88888888#",
                        }
                    ]
                return [
                    {
                        "小区": "诸葛龙吟院",
                        "房号": "13-1101",
                        "密码": "336699#",
                    }
                ]

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_store = main.wecom_kf_context_store
            previous_inventory = main.inventory
            fake_client = FakeKfClient()
            video_path = Path("room_database/video/房源素材/诸葛龙吟院13-1101/视频.mp4")
            try:
                main.wecom_kf = fake_client
                main.inventory = FakeInventory()
                main.wecom_kf_context_store = WeComKfContextStore(
                    path=Path(directory) / "context.json"
                )
                main.wecom_kf_conversation_memory.clear()
                main._remember_kf_media_context(
                    "kf_xxx",
                    "wm_xxx",
                    video_paths=[video_path],
                    video_urls=["https://example.com/video.mp4"],
                )

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "香柠颜家府2-2-1401B怎么看房"},
                    }
                )

                self.assertEqual(len(fake_client.texts), 1)
                self.assertIn("香柠颜家府2-2-1401B看房密码是：88888888#", fake_client.texts[0])
                self.assertNotIn("诸葛龙吟院", fake_client.texts[0])
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.inventory = previous_inventory
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()

    async def test_generic_viewing_followup_after_multiple_videos_lists_all_passwords(self) -> None:
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
            async def search(self, query: str, limit: int = 8) -> list[dict]:
                return [
                    {"小区": "香柠颜家府", "房号": "2-2-1401B", "密码": "218619#"},
                    {"小区": "香柠颜家府", "房号": "3-1-701A", "密码": "336699#"},
                ]

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_store = main.wecom_kf_context_store
            previous_inventory = main.inventory
            fake_client = FakeKfClient()
            try:
                main.wecom_kf = fake_client
                main.inventory = FakeInventory()
                main.wecom_kf_context_store = WeComKfContextStore(
                    path=Path(directory) / "context.json"
                )
                main.wecom_kf_conversation_memory.clear()
                main._remember_kf_media_context(
                    "kf_xxx",
                    "wm_xxx",
                    video_paths=[
                        Path("room_database/video/香柠颜家府2-2-1401B/视频.mp4"),
                        Path("room_database/video/香柠颜家府3-1-701A/视频.mp4"),
                    ],
                    video_urls=[
                        "https://example.com/2-2-1401B.mp4",
                        "https://example.com/3-1-701A.mp4",
                    ],
                )

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "怎么看房的"},
                    }
                )

                self.assertEqual(len(fake_client.texts), 1)
                self.assertIn("香柠颜家府2-2-1401B：218619#", fake_client.texts[0])
                self.assertIn("香柠颜家府3-1-701A：336699#", fake_client.texts[0])
                self.assertIn("刚发的这几套", fake_client.texts[0])
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.inventory = previous_inventory
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()

    async def test_dynamic_password_viewing_query_sends_contact_numbers(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.texts: list[str] = []

            async def send_text(self, open_kfid: str, external_userid: str, content: str) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        class FakeInventory:
            async def search(self, query: str, limit: int = 8) -> list[dict]:
                return [{"小区": "诸葛龙吟院", "房号": "13-1101", "密码": "动态密码"}]

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_store = main.wecom_kf_context_store
            previous_inventory = main.inventory
            try:
                main.wecom_kf = FakeKfClient()
                main.inventory = FakeInventory()
                main.wecom_kf_context_store = WeComKfContextStore(path=Path(directory) / "context.json")
                main.wecom_kf_conversation_memory.clear()

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "诸葛怎么看房的"},
                    }
                )

                self.assertIn("诸葛龙吟院13-1101是动态密码", main.wecom_kf.texts[0])
                self.assertIn("18758141785", main.wecom_kf.texts[0])
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.inventory = previous_inventory
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()

    async def test_not_vacant_password_field_sends_booking_contact(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.texts: list[str] = []

            async def send_text(self, open_kfid: str, external_userid: str, content: str) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        class FakeInventory:
            async def search(self, query: str, limit: int = 8) -> list[dict]:
                return [{"小区": "长木府", "房号": "3-1002B", "密码": "6.14空出"}]

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_store = main.wecom_kf_context_store
            previous_inventory = main.inventory
            try:
                main.wecom_kf = FakeKfClient()
                main.inventory = FakeInventory()
                main.wecom_kf_context_store = WeComKfContextStore(path=Path(directory) / "context.json")
                main.wecom_kf_conversation_memory.clear()

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "长木府3-1002B怎么看房"},
                    }
                )

                self.assertIn("长木府3-1002B6.14空出", main.wecom_kf.texts[0])
                self.assertIn("目前还未空出", main.wecom_kf.texts[0])
                self.assertIn("预约", main.wecom_kf.texts[0])
                self.assertIn("19941091943", main.wecom_kf.texts[0])
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.inventory = previous_inventory
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

        class FakeInventory:
            async def search(self, content: str, limit: int = 8) -> list[dict]:
                return [{"小区": "小洋坝", "房号": "三区12-1003-2"}]

        class FakeMediaStore:
            def __init__(self, video_path: Path) -> None:
                self.video_path = video_path

            def list_room_database_videos(self, query: str, limit: int = 6) -> list[Path]:
                if "小洋坝" in query and "三区12-1003-2" in query:
                    return [self.video_path]
                return []

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_store = main.wecom_kf_context_store
            previous_inventory = main.inventory
            previous_media_store = main.media_store
            previous_delay = main.settings.wecom_kf_satisfaction_delay_seconds
            fake_client = FakeKfClient()
            video_path = Path("room_database/video/小洋坝三区12-1003-2/微信视频.mp4")
            try:
                main.wecom_kf = fake_client
                main.wecom_kf_context_store = WeComKfContextStore(
                    path=Path(directory) / "context.json"
                )
                main.inventory = FakeInventory()
                main.media_store = FakeMediaStore(video_path)
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
                main.inventory = previous_inventory
                main.media_store = previous_media_store
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
            async def search(self, content: str, limit: int = 8) -> list[dict]:
                return [{"小区": "小洋坝家园", "房号": "二区6-801-3"}]

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
            video_path = Path("room_database/video/小洋坝家园二区6-801-3/video.mp4")
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

                self.assertEqual(fake_client.texts, ["这是小洋坝家园二区6-801-3的视频。"])
                self.assertEqual(fake_client.videos, [video_path])
                self.assertIn("小洋坝家园", fake_media_store.queries[0])
                self.assertIn("二区6-801-3", fake_media_store.queries[0])
                self.assertEqual(fake_media_store.limits[0], 1)
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.inventory = previous_inventory
                main.media_store = previous_media_store
                main.settings.wecom_kf_satisfaction_delay_seconds = previous_delay
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()

    async def test_video_correction_uses_latest_room_detail_before_cached_images(self) -> None:
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
            async def search(self, content: str, limit: int = 8) -> list[dict]:
                return [{"小区": "华丰人家", "房号": "8-603"}]

        class FakeMediaStore:
            def __init__(self, video_path: Path) -> None:
                self.video_path = video_path
                self.queries: list[str] = []

            def list_room_database_videos(self, query: str, limit: int = 6) -> list[Path]:
                self.queries.append(query)
                if "华丰人家" in query and "8-603" in query:
                    return [self.video_path]
                return []

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_store = main.wecom_kf_context_store
            previous_inventory = main.inventory
            previous_media_store = main.media_store
            fake_client = FakeKfClient()
            video_path = Path("room_database/video/华丰人家8-603/视频.mp4")
            fake_media_store = FakeMediaStore(video_path)
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
                        "image_paths": [str(Path("room_database/inventory_1.png"))],
                        "recent_messages": [
                            {
                                "role": "客服",
                                "content": (
                                    "华丰人家 8-603 这套是 65㎡法式中古风一室一厅，"
                                    "我这边暂时没有这套的视频。"
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
                        "text": {"content": "你明明有这个视频啊"},
                    }
                )

                self.assertEqual(fake_client.texts, ["这是华丰人家8-603的视频。"])
                self.assertEqual(fake_client.videos, [video_path])
                self.assertIn("华丰人家", fake_media_store.queries[0])
                self.assertIn("8-603", fake_media_store.queries[0])
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.inventory = previous_inventory
                main.media_store = previous_media_store
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()

    async def test_video_request_checks_inventory_before_room_database_media(self) -> None:
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
            def __init__(self) -> None:
                self.queries: list[str] = []
                self.limits: list[int] = []

            async def search(self, query: str, limit: int = 8) -> list[dict]:
                self.queries.append(query)
                self.limits.append(limit)
                return [
                    {"小区": "棠润府", "房号": "1-602B", "价格": "1700"},
                    {"小区": "棠润府", "房号": "12-2-1202A", "价格": "1700"},
                ]

        class FakeMediaStore:
            def __init__(self) -> None:
                self.queries: list[str] = []
                self.old = Path("room_database/video/棠润府10-1004C/视频.mp4")
                self.first = Path("room_database/video/棠润府1-602B/视频.mp4")
                self.second = Path("room_database/video/棠润府12-2-1202A/视频.mp4")

            def list_room_database_videos(self, query: str, limit: int = 6) -> list[Path]:
                self.queries.append(query)
                if "10-1004C" in query:
                    return [self.old]
                if "1-602B" in query:
                    return [self.first]
                if "12-2-1202A" in query:
                    return [self.second]
                return []

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_store = main.wecom_kf_context_store
            previous_inventory = main.inventory
            previous_media_store = main.media_store
            fake_client = FakeKfClient()
            fake_inventory = FakeInventory()
            fake_media_store = FakeMediaStore()
            try:
                main.wecom_kf = fake_client
                main.wecom_kf_context_store = WeComKfContextStore(
                    path=Path(directory) / "context.json"
                )
                main.inventory = fake_inventory
                main.media_store = fake_media_store
                main.wecom_kf_conversation_memory.clear()

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "棠润府1700的视频发一下啊"},
                    }
                )

                self.assertEqual(fake_inventory.limits, [main.KF_VIDEO_SEND_LIMIT])
                self.assertEqual(fake_client.videos, [fake_media_store.first, fake_media_store.second])
                self.assertIn("棠润府1-602B", fake_media_store.queries)
                self.assertIn("棠润府12-2-1202A", fake_media_store.queries)
                self.assertNotIn(fake_media_store.old, fake_client.videos)
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.inventory = previous_inventory
                main.media_store = previous_media_store
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()

    async def test_community_video_request_checks_five_inventory_rooms(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.videos: list[Path] = []
                self.texts: list[str] = []

            async def send_video(self, open_kfid: str, external_userid: str, video_path: Path) -> dict:
                self.videos.append(video_path)
                return {"errcode": 0}

            async def send_text(self, open_kfid: str, external_userid: str, content: str) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        class FakeInventory:
            def __init__(self) -> None:
                self.limits: list[int] = []

            async def search(self, query: str, limit: int = 8) -> list[dict]:
                self.limits.append(limit)
                return [
                    {"小区": "皋塘运都", "房号": "12-1-1802"},
                    {"小区": "皋塘运都", "房号": "12-2-401"},
                    {"小区": "皋塘运都", "房号": "16-1-804"},
                    {"小区": "皋塘运都", "房号": "16-1-805"},
                    {"小区": "皋塘运都", "房号": "16-1-1003"},
                ][:limit]

        class FakeMediaStore:
            def __init__(self) -> None:
                self.paths = {
                    room: Path(f"room_database/video/皋塘运都{room}/视频.mp4")
                    for room in ("12-1-1802", "16-1-804", "16-1-805", "16-1-1003")
                }

            def list_room_database_videos(self, query: str, limit: int = 6) -> list[Path]:
                for room, path in self.paths.items():
                    if room in query:
                        return [path]
                return []

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_store = main.wecom_kf_context_store
            previous_inventory = main.inventory
            previous_media_store = main.media_store
            fake_client = FakeKfClient()
            fake_inventory = FakeInventory()
            fake_media_store = FakeMediaStore()
            try:
                main.wecom_kf = fake_client
                main.wecom_kf_context_store = WeComKfContextStore(path=Path(directory) / "context.json")
                main.inventory = fake_inventory
                main.media_store = fake_media_store
                main.wecom_kf_conversation_memory.clear()

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "皋塘运都视频发一下"},
                    }
                )

                self.assertEqual(fake_inventory.limits, [main.KF_VIDEO_SEND_LIMIT])
                self.assertEqual(
                    fake_client.videos,
                    [
                        fake_media_store.paths["12-1-1802"],
                        fake_media_store.paths["16-1-804"],
                    ],
                )
                self.assertIn("这些房源表里还在，其中皋塘运都12-2-401视频暂时没有。", fake_client.texts[0])
                self.assertTrue(any("视频比较多" in text for text in fake_client.texts))
                self.assertTrue(any("5分钟后" in text for text in fake_client.texts))
                self.assertNotIn("https://", "\n".join(fake_client.texts))
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.inventory = previous_inventory
                main.media_store = previous_media_store
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()

    async def test_video_request_replies_rented_out_when_inventory_has_no_match(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.videos: list[Path] = []
                self.texts: list[str] = []

            async def send_video(self, open_kfid: str, external_userid: str, video_path: Path) -> dict:
                self.videos.append(video_path)
                return {"errcode": 0}

            async def send_text(self, open_kfid: str, external_userid: str, content: str) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        class FakeInventory:
            async def search(self, query: str, limit: int = 8) -> list[dict]:
                return []

        class FakeMediaStore:
            def list_room_database_videos(self, query: str, limit: int = 6) -> list[Path]:
                return [Path("room_database/video/棠润府10-1004C/视频.mp4")]

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_store = main.wecom_kf_context_store
            previous_inventory = main.inventory
            previous_media_store = main.media_store
            fake_client = FakeKfClient()
            try:
                main.wecom_kf = fake_client
                main.wecom_kf_context_store = WeComKfContextStore(
                    path=Path(directory) / "context.json"
                )
                main.inventory = FakeInventory()
                main.media_store = FakeMediaStore()
                main.wecom_kf_conversation_memory.clear()

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "棠润府10-1004C视频发一下"},
                    }
                )

                self.assertEqual(fake_client.videos, [])
                self.assertEqual(len(fake_client.texts), 1)
                self.assertIn("已经租掉了", fake_client.texts[0])
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.inventory = previous_inventory
                main.media_store = previous_media_store
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()

    async def test_yanglefu_video_request_does_not_send_stale_media_when_not_in_inventory(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.videos: list[Path] = []
                self.texts: list[str] = []

            async def send_video(self, open_kfid: str, external_userid: str, video_path: Path) -> dict:
                self.videos.append(video_path)
                return {"errcode": 0}

            async def send_text(self, open_kfid: str, external_userid: str, content: str) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

        class FakeInventory:
            async def search(self, query: str, limit: int = 8) -> list[dict]:
                return []

        class FakeMediaStore:
            def __init__(self) -> None:
                self.queries: list[str] = []
                self.stale_video = Path("room_database/video/杨乐府1-101/视频.mp4")

            def list_room_database_videos(self, query: str, limit: int = 6) -> list[Path]:
                self.queries.append(query)
                if "杨乐府" in query:
                    return [self.stale_video]
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

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "杨乐府的视频发一下"},
                    }
                )

                self.assertEqual(fake_client.videos, [])
                self.assertEqual(fake_media_store.queries, [])
                self.assertEqual(len(fake_client.texts), 1)
                self.assertIn("杨乐府", fake_client.texts[0])
                self.assertIn("已经租掉了", fake_client.texts[0])
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.inventory = previous_inventory
                main.media_store = previous_media_store
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
            async def search(self, content: str, limit: int = 8) -> list[dict]:
                if "华丰人家" in content:
                    return [{"小区": "华丰人家", "房号": "8-603"}]
                return [
                    {"小区": "永佳新苑", "房号": "2-703"},
                ]

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
                self.assertEqual(fake_media_store.limits[:2], [1, 1])
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.inventory = previous_inventory
                main.media_store = previous_media_store
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()

    async def test_polite_video_followup_sends_recent_listed_room_videos(self) -> None:
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
            async def search(self, content: str, limit: int = 8) -> list[dict]:
                if "杨家府" not in content and "10-1-304" not in content and "1-202" not in content:
                    return []
                return [
                    {"小区": "杨家府（兴业杨家府）", "房号": "10-1-304"},
                    {"小区": "杨家府（兴业杨家府）", "房号": "1-202"},
                ][:limit]

            def format_rows(self, rows: list[dict]) -> str:
                return ""

            async def snapshot(self) -> str:
                return "暂无"

        class FakeMediaStore:
            def __init__(self) -> None:
                self.links = ["https://example.com/old-original.mp4"]
                self.room_10304 = Path("room_database/video/杨家府10-1-304/视频.mp4")
                self.room_1202 = Path("room_database/video/杨家府1-202/视频.mp4")

            def list_for_rooms(self, rooms: list[dict]) -> list:
                return []

            def public_urls(self, media: list) -> tuple[list[str], list[str]]:
                return [], []

            def list_room_database_videos(self, query: str, limit: int = 6) -> list[Path]:
                paths = []
                if "10-1-304" in query:
                    paths.append(self.room_10304)
                if "1-202" in query:
                    paths.append(self.room_1202)
                return paths[:limit]

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
                        "video_urls": ["https://example.com/old-original.mp4"],
                        "recent_messages": [
                            {"role": "客户", "content": "杨家府的还在吗"},
                            {
                                "role": "客服",
                                "content": (
                                    "杨家府（兴业杨家府）我这边看到还有两套：\n"
                                    "1. 房号10-1-304，一室一厅，押一付4500/押二付4200，密码88888888#，民用水电。\n"
                                    "2. 房号1-202，一室一厅，押一付4200/押一付3800，密码336699#，民用水电。\n"
                                    "这两套都是65㎡整租，一年起租。需要看视频或者具体细节直接说，我发你。"
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
                        "text": {"content": "视频麻烦发我一下"},
                    }
                )

                self.assertEqual(
                    fake_client.texts,
                    [
                        "这是杨家府10-1-304的视频。",
                        "这是杨家府1-202的视频。",
                    ],
                )
                self.assertEqual(
                    fake_client.videos,
                    [fake_media_store.room_10304, fake_media_store.room_1202],
                )
                self.assertNotIn("old-original.mp4", "\n".join(fake_client.texts))
                self.assertNotIn("已经租掉了", "\n".join(fake_client.texts))
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.inventory = previous_inventory
                main.media_store = previous_media_store
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()

    async def test_final_validation_fixes_false_missing_video_reply_and_sends_video(self) -> None:
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
            async def search(self, content: str, limit: int = 8) -> list[dict]:
                return [
                    {
                        "小区": "大华海派风景",
                        "房号": "2-1-402A",
                        "户型": "朝南一室一厅燃气独立厨卫",
                        "视频索引": "素材库唯一索引Z9",
                    }
                ][:limit]

            def format_rows(self, rows: list[dict]) -> str:
                return "大华海派风景2-1-402A，朝南一室一厅燃气独立厨卫"

            async def snapshot(self) -> str:
                return "暂无"

        class FakeMediaStore:
            def __init__(self) -> None:
                self.video_path = Path("room_database/video/大华海派风景2-1-402A/视频.mp4")

            def list_for_rooms(self, rooms: list[dict]) -> list:
                return []

            def public_urls(self, media: list) -> tuple[list[str], list[str]]:
                return [], []

            def list_room_database_videos(self, query: str, limit: int = 6) -> list[Path]:
                if "素材库唯一索引Z9" in query:
                    return [self.video_path]
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
                return ReplyPlan(
                    text=(
                        "这套是大华海派风景 2-1-402A，朝南一室一厅带燃气和独立厨卫。\n"
                        "我这边暂时没找到这套的视频，需要人工再确认一下素材。"
                    )
                )

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_store = main.wecom_kf_context_store
            previous_inventory = main.inventory
            previous_media_store = main.media_store
            previous_generator = main.reply_generator
            previous_key = main.settings.dashscope_api_key
            fake_client = FakeKfClient()
            fake_media_store = FakeMediaStore()
            try:
                main.wecom_kf = fake_client
                main.wecom_kf_context_store = WeComKfContextStore(
                    path=Path(directory) / "context.json"
                )
                main.inventory = FakeInventory()
                main.media_store = fake_media_store
                main.reply_generator = FakeReplyGenerator()
                main.settings.dashscope_api_key = ""
                main.wecom_kf_conversation_memory.clear()

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "就是这套"},
                    }
                )

                self.assertEqual(fake_client.videos, [fake_media_store.video_path])
                self.assertNotIn("暂时没找到", fake_client.texts[0])
                self.assertIn("有对应视频", fake_client.texts[0])
                self.assertEqual(fake_client.texts[1], "这是大华海派风景2-1-402A的视频。")
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.inventory = previous_inventory
                main.media_store = previous_media_store
                main.reply_generator = previous_generator
                main.settings.dashscope_api_key = previous_key
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

    async def test_batch_room_database_videos_sends_two_native_then_links(self) -> None:
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
            previous_root = main.settings.room_database_path
            previous_base_url = main.settings.public_base_url
            fake_client = FakeKfClient()
            root = Path(directory)
            video_paths = [
                root / "video" / f"小区{index}" / "视频.mp4"
                for index in range(4)
            ]
            for path in video_paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"video")
            try:
                main.wecom_kf = fake_client
                main.settings.room_database_path = root
                main.settings.public_base_url = "https://example.com"

                sent = await main._send_kf_room_database_videos("kf_xxx", "wm_xxx", video_paths)

                self.assertTrue(sent)
                self.assertEqual(fake_client.videos, video_paths[:2])
                self.assertIn("视频比较多", fake_client.texts[0])
                self.assertIn("5分钟后", fake_client.texts[0])
                self.assertIn("这是小区0的视频。", fake_client.texts[1])
                self.assertIn("这是小区1的视频。", fake_client.texts[2])
                self.assertEqual(len(fake_client.texts), 3)
                self.assertNotIn("https://", "\n".join(fake_client.texts))
            finally:
                main.wecom_kf = previous_client
                main.settings.room_database_path = previous_root
                main.settings.public_base_url = previous_base_url

    async def test_send_limit_records_pending_video_without_sent_dialog(self) -> None:
        class FakeKfClient:
            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                raise WeComKfSendLimitError("send msg count limit")

            async def send_video(
                self,
                open_kfid: str,
                external_userid: str,
                video_path: Path,
            ) -> dict:
                return {"errcode": 0}

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_store = main.wecom_kf_context_store
            previous_root = main.settings.room_database_path
            previous_base_url = main.settings.public_base_url
            root = Path(directory)
            video_path = root / "video" / "小区1" / "视频.mp4"
            video_path.parent.mkdir(parents=True, exist_ok=True)
            video_path.write_bytes(b"video")
            try:
                main.wecom_kf = FakeKfClient()
                main.wecom_kf_context_store = WeComKfContextStore(path=root / "context.json")
                main.wecom_kf_conversation_memory.clear()
                main.settings.room_database_path = root
                main.settings.public_base_url = "https://example.com"

                sent = await main._send_kf_room_database_videos("kf_xxx", "wm_xxx", [video_path])

                self.assertFalse(sent)
                context = main.wecom_kf_context_store.get("kf_xxx:wm_xxx")
                self.assertIn("send_limited", context)
                self.assertEqual(context["send_limited"]["video_urls"], [])
                dialog_text = "\n".join(item["content"] for item in context["recent_messages"])
                self.assertNotIn("已直接发送相关视频", dialog_text)
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.wecom_kf_conversation_memory.clear()
                main.settings.room_database_path = previous_root
                main.settings.public_base_url = previous_base_url

    async def test_send_limit_recovery_prompt_sends_pending_video_links_first(self) -> None:
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
            fake_client = FakeKfClient()
            try:
                main.wecom_kf = fake_client
                main.wecom_kf_context_store = WeComKfContextStore(path=Path(directory) / "context.json")
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_context_store.save(
                    "kf_xxx:wm_xxx",
                    {
                        "send_limited": {
                            "triggered_at": 123.0,
                            "summary": "批量视频触发限流",
                            "video_urls": ["https://example.com/video-1.mp4"],
                        },
                        "updated_at": main.time.time(),
                    },
                )

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
                self.assertIn("刚才微信客服发送次数到上限了", fake_client.texts[0])
                self.assertIn("5分钟后", fake_client.texts[0])
                self.assertNotIn("https://", fake_client.texts[0])
                context = main.wecom_kf_context_store.get("kf_xxx:wm_xxx")
                self.assertNotIn("send_limited", context)
                self.assertIn("刚才微信客服发送次数到上限了", context["recent_messages"][-1]["content"])
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.wecom_kf_conversation_memory.clear()

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
        previous_cached = main.cached_wecom_video
        fake_client = FakeKfClient()
        original = Path("room_database/video/华丰人家8-603/original.mp4")
        compressed = Path("room_database/video/华丰人家8-603/.wecom_cache/original.wecom.mp4")
        try:
            main.wecom_kf = fake_client
            main.needs_wecom_video_transcode = lambda path: True
            main.cached_wecom_video = lambda path: compressed

            sent = await main._send_kf_room_database_videos("kf_xxx", "wm_xxx", [original])

            self.assertTrue(sent)
            self.assertEqual(fake_client.videos, [compressed])
            self.assertEqual(fake_client.texts, ["这是华丰人家8-603的视频。"])
        finally:
            main.wecom_kf = previous_client
            main.needs_wecom_video_transcode = previous_needs
            main.cached_wecom_video = previous_cached

    async def test_large_uncached_video_falls_back_to_link_after_transcode_timeout(self) -> None:
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
            previous_needs = main.needs_wecom_video_transcode
            previous_cached = main.cached_wecom_video
            previous_prepare = main.prepare_wecom_video
            previous_root = main.settings.room_database_path
            previous_base_url = main.settings.public_base_url
            fake_client = FakeKfClient()
            video_path = Path(directory) / "video" / "诸葛龙吟院13-1101" / "视频.mp4"
            video_path.parent.mkdir(parents=True)
            video_path.write_bytes(b"video")
            try:
                main.wecom_kf = fake_client
                main.needs_wecom_video_transcode = lambda path: True
                main.cached_wecom_video = lambda path: None
                main.prepare_wecom_video = lambda path, force=False, timeout=180: (_ for _ in ()).throw(
                    TimeoutError(f"timeout={timeout}")
                )
                main.settings.room_database_path = Path(directory)
                main.settings.public_base_url = "https://example.com"

                sent = await main._send_kf_room_database_videos("kf_xxx", "wm_xxx", [video_path])

                self.assertTrue(sent)
                self.assertEqual(len(fake_client.texts), 1)
                self.assertIn("视频直发失败", fake_client.texts[0])
                self.assertIn("5分钟后", fake_client.texts[0])
                self.assertNotIn("https://", fake_client.texts[0])
            finally:
                main.wecom_kf = previous_client
                main.needs_wecom_video_transcode = previous_needs
                main.cached_wecom_video = previous_cached
                main.prepare_wecom_video = previous_prepare
                main.settings.room_database_path = previous_root
                main.settings.public_base_url = previous_base_url

    async def test_large_uncached_video_waits_for_quick_transcode_then_sends_native_video(self) -> None:
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
        previous_cached = main.cached_wecom_video
        previous_prepare = main.prepare_wecom_video
        fake_client = FakeKfClient()
        original = Path("room_database/video/诸葛龙吟院13-1101/original.mp4")
        compressed = Path("room_database/video/诸葛龙吟院13-1101/.wecom_cache/original.wecom.mp4")
        seen_timeouts: list[int] = []
        try:
            main.wecom_kf = fake_client
            main.needs_wecom_video_transcode = lambda path: True
            main.cached_wecom_video = lambda path: None

            def quick_prepare(path: Path, force: bool = False, timeout: int = 180) -> Path:
                seen_timeouts.append(timeout)
                return compressed

            main.prepare_wecom_video = quick_prepare

            sent = await main._send_kf_room_database_videos("kf_xxx", "wm_xxx", [original])

            self.assertTrue(sent)
            self.assertEqual(fake_client.videos, [compressed])
            self.assertEqual(fake_client.texts, ["这是诸葛龙吟院13-1101的视频。"])
            self.assertEqual(seen_timeouts, [main.KF_VIDEO_TRANSCODE_WAIT_SECONDS])
        finally:
            main.wecom_kf = previous_client
            main.needs_wecom_video_transcode = previous_needs
            main.cached_wecom_video = previous_cached
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
            main.prepare_wecom_video = lambda path, force=False, timeout=180: compressed

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
                main.prepare_wecom_video = lambda path, force=False, timeout=180: (_ for _ in ()).throw(
                    RuntimeError("transcode failed")
                )
                video_path = Path(directory) / "video" / "琬秋铭府1-1803" / "视频.mp4"
                video_path.parent.mkdir(parents=True)
                video_path.write_bytes(b"not-a-real-video")

                sent = await main._send_kf_room_database_videos("kf_xxx", "wm_xxx", [video_path])

                self.assertTrue(sent)
                self.assertEqual(len(fake_client.texts), 1)
                self.assertIn("视频直发失败", fake_client.texts[0])
                self.assertIn("5分钟后", fake_client.texts[0])
                self.assertNotIn("https://", fake_client.texts[0])
                self.assertNotIn("这是琬秋铭府1-1803的视频", fake_client.texts[0])
            finally:
                main.wecom_kf = previous_client
                main.settings.room_database_path = previous_root
                main.settings.public_base_url = previous_base_url
                main.prepare_wecom_video = previous_prepare


class WeComKfImagePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_room_image_request_does_not_send_room_images(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.texts: list[str] = []
                self.images: list[Path] = []

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

            async def send_image(
                self,
                open_kfid: str,
                external_userid: str,
                image_path: Path,
            ) -> dict:
                self.images.append(image_path)
                return {"errcode": 0}

        previous_client = main.wecom_kf
        fake_client = FakeKfClient()
        try:
            main.wecom_kf = fake_client
            main.wecom_kf_conversation_memory.clear()

            await main.handle_kf_message(
                {
                    "msgid": "msg-1",
                    "open_kfid": "kf_xxx",
                    "external_userid": "wm_xxx",
                    "origin": 3,
                    "msgtype": "text",
                    "text": {"content": "华丰人家照片发一下"},
                }
            )

            self.assertEqual(fake_client.images, [])
            self.assertEqual(len(fake_client.texts), 1)
            self.assertIn("房间图片这边不单独发送", fake_client.texts[0])
            self.assertIn("只发视频", fake_client.texts[0])
        finally:
            main.wecom_kf = previous_client
            main.wecom_kf_conversation_memory.clear()
            main.wecom_kf_idle_sequences.clear()

    async def test_inventory_table_image_request_still_sends_inventory_png(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.texts: list[str] = []
                self.images: list[Path] = []

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

            async def send_image(
                self,
                open_kfid: str,
                external_userid: str,
                image_path: Path,
            ) -> dict:
                self.images.append(image_path)
                return {"errcode": 0}

        async def noop_refresh() -> bool:
            return True

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_root = main.settings.room_database_path
            previous_glob = main.settings.inventory_image_glob
            previous_refresh = main._refresh_inventory_images_if_needed
            fake_client = FakeKfClient()
            image_path = Path(directory) / "inventory_01.png"
            image_path.write_bytes(b"image")
            try:
                main.wecom_kf = fake_client
                main.settings.room_database_path = Path(directory) / "room_database"
                main.settings.inventory_image_glob = str(image_path)
                main._refresh_inventory_images_if_needed = noop_refresh
                main.wecom_kf_conversation_memory.clear()

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "房源表图片发我"},
                    }
                )

                self.assertEqual(fake_client.images, [image_path])
                self.assertEqual(fake_client.texts, [])
            finally:
                main.wecom_kf = previous_client
                main.settings.room_database_path = previous_root
                main.settings.inventory_image_glob = previous_glob
                main._refresh_inventory_images_if_needed = previous_refresh
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()

    async def test_inventory_table_image_request_does_not_send_old_png_when_refresh_fails(self) -> None:
        class FakeKfClient:
            def __init__(self) -> None:
                self.texts: list[str] = []
                self.images: list[Path] = []

            async def send_text(
                self,
                open_kfid: str,
                external_userid: str,
                content: str,
            ) -> dict:
                self.texts.append(content)
                return {"errcode": 0}

            async def send_image(
                self,
                open_kfid: str,
                external_userid: str,
                image_path: Path,
            ) -> dict:
                self.images.append(image_path)
                return {"errcode": 0}

        async def failed_refresh() -> bool:
            return False

        with tempfile.TemporaryDirectory() as directory:
            previous_client = main.wecom_kf
            previous_root = main.settings.room_database_path
            previous_glob = main.settings.inventory_image_glob
            previous_refresh = main._refresh_inventory_images_if_needed
            fake_client = FakeKfClient()
            image_path = Path(directory) / "inventory_01.png"
            image_path.write_bytes(b"old-image")
            try:
                main.wecom_kf = fake_client
                main.settings.room_database_path = Path(directory) / "room_database"
                main.settings.inventory_image_glob = str(image_path)
                main._refresh_inventory_images_if_needed = failed_refresh
                main.wecom_kf_conversation_memory.clear()

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "房源表发一下"},
                    }
                )

                self.assertEqual(fake_client.images, [])
                self.assertEqual(len(fake_client.texts), 1)
                self.assertIn("先不发旧表", fake_client.texts[0])
            finally:
                main.wecom_kf = previous_client
                main.settings.room_database_path = previous_root
                main.settings.inventory_image_glob = previous_glob
                main._refresh_inventory_images_if_needed = previous_refresh
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()

    async def test_original_video_without_context_asks_for_room_reference(self) -> None:
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
            fake_client = FakeKfClient()
            try:
                main.wecom_kf = fake_client
                main.wecom_kf_context_store = WeComKfContextStore(
                    path=Path(directory) / "context.json"
                )
                main.wecom_kf_conversation_memory.clear()

                await main.handle_kf_message(
                    {
                        "msgid": "msg-1",
                        "open_kfid": "kf_xxx",
                        "external_userid": "wm_xxx",
                        "origin": 3,
                        "msgtype": "text",
                        "text": {"content": "有没有更清楚一点的"},
                    }
                )

                self.assertEqual(len(fake_client.texts), 1)
                self.assertIn("小区和房号", fake_client.texts[0])
                self.assertNotIn("已经租掉", fake_client.texts[0])
            finally:
                main.wecom_kf = previous_client
                main.wecom_kf_context_store = previous_store
                main.wecom_kf_conversation_memory.clear()
                main.wecom_kf_idle_sequences.clear()


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
