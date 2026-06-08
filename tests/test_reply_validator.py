import unittest
from pathlib import Path

from app.config import settings
from app.services.reply_validator import ReplyValidationDraft, ReplyValidator


class ReplyValidatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixes_missing_video_claim_when_material_exists(self) -> None:
        previous_key = settings.dashscope_api_key
        try:
            settings.dashscope_api_key = ""
            validator = ReplyValidator()
            video_path = Path("room_database/video/大华海派风景2-1-402A/视频.mp4")

            result = await validator.validate(
                ReplyValidationDraft(
                    customer_text="就是这套",
                    reply_text=(
                        "这套是大华海派风景 2-1-402A，朝南一室一厅。\n"
                        "我这边暂时没找到这套的视频，需要人工再确认一下素材。"
                    ),
                    inventory_rows=[{"小区": "大华海派风景", "房号": "2-1-402A"}],
                    available_video_paths=[video_path],
                )
            )

            self.assertFalse(result.ok)
            self.assertIn("真实素材库存在视频", result.problems[0])
            self.assertNotIn("暂时没找到", result.reply_text)
            self.assertIn("有对应视频", result.reply_text)
            self.assertEqual(result.extra_video_paths, [video_path])
        finally:
            settings.dashscope_api_key = previous_key

    async def test_fixes_soft_missing_video_claim_when_material_exists(self) -> None:
        previous_key = settings.dashscope_api_key
        try:
            settings.dashscope_api_key = ""
            validator = ReplyValidator()
            video_path = Path("room_database/video/大华海派风景2-1-402A/视频.mp4")

            for missing_claim in (
                "视频我这边暂时没挂上，需要再确认下，你要不要先看看详情？",
                "视频我这边暂时没素材，需要再确认下。",
            ):
                with self.subTest(missing_claim=missing_claim):
                    result = await validator.validate(
                        ReplyValidationDraft(
                            customer_text="大华有房吗",
                            reply_text=(
                                "有的，大华海派风景这套刚空出来：2-1-402A，"
                                "朝南一室一厅带燃气，独立厨卫。"
                                "押一付1600，押二付1500。"
                                f"{missing_claim}"
                            ),
                            inventory_rows=[{"小区": "大华海派风景", "房号": "2-1-402A"}],
                            available_video_paths=[video_path],
                        )
                    )

                    self.assertFalse(result.ok)
                    self.assertIn("真实素材库存在视频", result.problems[0])
                    self.assertIn("大华海派风景", result.reply_text)
                    self.assertNotIn("暂时没挂上", result.reply_text)
                    self.assertNotIn("暂时没素材", result.reply_text)
                    self.assertIn("有对应视频", result.reply_text)
                    self.assertEqual(result.extra_video_paths, [video_path])
        finally:
            settings.dashscope_api_key = previous_key

    async def test_blocks_video_that_does_not_match_inventory_room(self) -> None:
        previous_key = settings.dashscope_api_key
        try:
            settings.dashscope_api_key = ""
            validator = ReplyValidator()

            result = await validator.validate(
                ReplyValidationDraft(
                    customer_text="华丰8-603视频发一下",
                    reply_text="已直接发送相关视频。",
                    inventory_rows=[{"小区": "华丰人家", "房号": "8-603"}],
                    send_video_paths=[Path("room_database/video/大华海派风景2-1-402A/视频.mp4")],
                    available_video_paths=[
                        Path("room_database/video/大华海派风景2-1-402A/视频.mp4")
                    ],
                )
            )

            self.assertFalse(result.ok)
            self.assertIn("不一致", result.problems[0])
            self.assertIn("避免发错房间", result.reply_text)
        finally:
            settings.dashscope_api_key = previous_key

    async def test_blocks_non_inventory_room_images(self) -> None:
        previous_key = settings.dashscope_api_key
        try:
            settings.dashscope_api_key = ""
            validator = ReplyValidator()

            result = await validator.validate(
                ReplyValidationDraft(
                    customer_text="华丰人家照片发一下",
                    reply_text="我把照片发你。",
                    send_image_paths=[Path("room_database/images/华丰人家8-603/1.jpg")],
                )
            )

            self.assertFalse(result.ok)
            self.assertIn("不允许发送房间图片素材", result.problems[0])
            self.assertIn("房间图片这边不单独发送", result.reply_text)
        finally:
            settings.dashscope_api_key = previous_key

    async def test_blocks_same_room_number_from_different_community(self) -> None:
        previous_key = settings.dashscope_api_key
        try:
            settings.dashscope_api_key = ""
            validator = ReplyValidator()

            result = await validator.validate(
                ReplyValidationDraft(
                    customer_text="华丰人家8-603视频发一下",
                    reply_text="已直接发送相关视频。",
                    inventory_rows=[{"小区": "华丰人家", "房号": "8-603"}],
                    send_video_paths=[Path("room_database/video/大华海派风景8-603/视频.mp4")],
                    available_video_paths=[
                        Path("room_database/video/大华海派风景8-603/视频.mp4")
                    ],
                )
            )

            self.assertFalse(result.ok)
            self.assertIn("不一致", result.problems[0])
        finally:
            settings.dashscope_api_key = previous_key

    async def test_allows_video_room_suffix_with_or_without_extra_hyphen(self) -> None:
        previous_key = settings.dashscope_api_key
        try:
            settings.dashscope_api_key = ""
            validator = ReplyValidator()
            video_path = Path("room_database/video/金昌苑2-2-1601E/视频.mp4")

            result = await validator.validate(
                ReplyValidationDraft(
                    customer_text="金昌苑视频发一下",
                    reply_text="已直接发送相关视频。",
                    inventory_rows=[{"小区": "金昌苑", "房号": "2-2-1601-E"}],
                    send_video_paths=[video_path],
                    available_video_paths=[video_path],
                )
            )

            self.assertTrue(result.ok)
        finally:
            settings.dashscope_api_key = previous_key

    async def test_allows_video_room_letter_a_for_legacy_dash_one_suffix(self) -> None:
        previous_key = settings.dashscope_api_key
        try:
            settings.dashscope_api_key = ""
            validator = ReplyValidator()
            video_path = Path("room_database/video/孔家埭和府1-1-901A/视频.mp4")

            result = await validator.validate(
                ReplyValidationDraft(
                    customer_text="孔家埭视频发一下",
                    reply_text="已直接发送相关视频。",
                    inventory_rows=[{"小区": "孔家埭和府", "房号": "1-1-901-1"}],
                    send_video_paths=[video_path],
                    available_video_paths=[video_path],
                )
            )

            self.assertTrue(result.ok)
        finally:
            settings.dashscope_api_key = previous_key

    async def test_blocks_available_claim_for_room_missing_from_latest_inventory(self) -> None:
        previous_key = settings.dashscope_api_key
        try:
            settings.dashscope_api_key = ""
            validator = ReplyValidator()

            result = await validator.validate(
                ReplyValidationDraft(
                    customer_text="星桥还有房子吗",
                    reply_text="星桥目前就锦绣嘉苑这套21-1801-2在租，押一付1880。",
                    inventory_rows=[],
                    reference_inventory_rows=[
                        {"小区": "棠润府", "房号": "1-602A"},
                        {"小区": "长木府", "房号": "3-1002B"},
                    ],
                )
            )

            self.assertFalse(result.ok)
            self.assertIn("最新房源表没有这些房号", result.problems[0])
            self.assertIn("最新房源表里查不到", result.reply_text)
            self.assertNotIn("21-1801-2在租", result.reply_text)
        finally:
            settings.dashscope_api_key = previous_key

    async def test_allows_available_claim_when_room_exists_in_latest_inventory(self) -> None:
        previous_key = settings.dashscope_api_key
        try:
            settings.dashscope_api_key = ""
            validator = ReplyValidator()

            result = await validator.validate(
                ReplyValidationDraft(
                    customer_text="棠润府还有吗",
                    reply_text="棠润府1-602A还在，押一付1700，6.15空出。",
                    inventory_rows=[{"小区": "棠润府", "房号": "1-602A"}],
                    reference_inventory_rows=[{"小区": "棠润府", "房号": "1-602A"}],
                )
            )

            self.assertTrue(result.ok)
            self.assertIn("棠润府1-602A还在", result.reply_text)
        finally:
            settings.dashscope_api_key = previous_key
