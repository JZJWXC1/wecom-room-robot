import unittest
import tempfile
from pathlib import Path

import pandas as pd

from app.config import settings
from app.services.feishu import FeishuClient
from app.services.inventory import InventoryService


class InventoryFuzzyMatchingTests(unittest.IsolatedAsyncioTestCase):
    async def test_format_rows_uses_canonical_community_display_name(self) -> None:
        inventory = InventoryService()

        text = inventory.format_rows(
            [
                {
                    "小区": "华丰新苑",
                    "房号": "20-1-504",
                    "户型": "两室一厅",
                    "押一付": "3800",
                }
            ]
        )

        self.assertIn("小区:华丰欣苑", text)
        self.assertNotIn("华丰新苑", text)

    async def test_fuzzy_matches_typoed_community_names(self) -> None:
        previous_source = settings.inventory_source
        try:
            settings.inventory_source = "local_cache"
            inventory = InventoryService()
            inventory._cache = pd.DataFrame(
                [
                    {
                        "小区": "永佳新苑",
                        "房号": "2-703",
                        "户型": "一室一厅",
                        "押一付": "4500",
                    },
                    {
                        "小区": "华丰人家",
                        "房号": "8-603",
                        "户型": "一室一厅",
                        "押一付": "4600",
                    },
                ]
            )

            rows = await inventory.search("永住新苑还有房吗", limit=3)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["小区"], "永佳新苑")
        finally:
            settings.inventory_source = previous_source

    async def test_inventory_cache_meta_records_source_path_hash_and_rows(self) -> None:
        previous_source = settings.inventory_source
        previous_cache_path = settings.inventory_cache_path
        previous_meta_path = settings.inventory_cache_meta_path
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                settings.inventory_source = "local_cache"
                settings.inventory_cache_path = root / "inventory_cache.csv"
                settings.inventory_cache_meta_path = root / "inventory_cache_meta.json"
                pd.DataFrame(
                    [
                        {"小区": "杭行荟", "房号": "2-1309"},
                        {"小区": "新柠长木府", "房号": "2-702B"},
                    ]
                ).to_csv(settings.inventory_cache_path, index=False, encoding="utf-8-sig")

                inventory = InventoryService()
                frame = await inventory.refresh()
                rows = await inventory.search("杭行荟", limit=3)

                self.assertEqual(2, len(frame))
                self.assertTrue(settings.inventory_cache_meta_path.exists())
                self.assertEqual("local_cache", inventory.cache_meta["source"])
                self.assertEqual(2, inventory.cache_meta["row_count"])
                self.assertTrue(inventory.cache_meta["hash"])
                self.assertIn("__inventory_meta", rows[0])
                self.assertEqual(inventory.cache_meta["hash"], rows[0]["__inventory_meta"]["hash"])
        finally:
            settings.inventory_source = previous_source
            settings.inventory_cache_path = previous_cache_path
            settings.inventory_cache_meta_path = previous_meta_path

    async def test_search_reloads_cache_when_sync_updates_csv(self) -> None:
        previous_source = settings.inventory_source
        previous_cache_path = settings.inventory_cache_path
        previous_meta_path = settings.inventory_cache_meta_path
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                settings.inventory_source = "local_cache"
                settings.inventory_cache_path = root / "inventory_cache.csv"
                settings.inventory_cache_meta_path = root / "inventory_cache_meta.json"
                pd.DataFrame(
                    [
                        {
                            "区域": "东新园 杭氧 新天地",
                            "小区": "东方茂",
                            "房号": "T3-1540",
                            "户型分类": "一室一厅",
                            "押一付一": "3800",
                            "押二付一": "3500",
                        }
                    ]
                ).to_csv(settings.inventory_cache_path, index=False, encoding="utf-8-sig")

                inventory = InventoryService()
                await inventory.refresh()
                self.assertEqual(await inventory.search("新天地4000-5000两室", limit=8), [])

                pd.DataFrame(
                    [
                        {
                            "区域": "东新园 杭氧 新天地",
                            "小区": "新柠长木府",
                            "房号": "3-1002A",
                            "户型分类": "两室一厅",
                            "押一付一": "4600",
                            "押二付一": "4300",
                        }
                    ]
                ).to_csv(settings.inventory_cache_path, index=False, encoding="utf-8-sig")

                rows = await inventory.search("新天地4000-5000两室", limit=8)

                self.assertEqual([row["房号"] for row in rows], ["3-1002A"])
        finally:
            settings.inventory_source = previous_source
            settings.inventory_cache_path = previous_cache_path
            settings.inventory_cache_meta_path = previous_meta_path

    async def test_spreadsheet_inventory_fills_merged_community_cells(self) -> None:
        previous_source = settings.inventory_source
        try:
            settings.inventory_source = "local_cache"
            inventory = InventoryService()
            frame = inventory._spreadsheet_values_to_frame(
                [
                    ["可芝麻信用免押金"],
                    ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "密码", "备注"],
                    ["东新园 杭氧 新天地", "杨乐府", "1-704B", "边套一室一厅朝南", "两室一厅", "3500", "3200", "6.6空出", "水30/月，电1元/度"],
                    ["", "", "9-1002", "（整）65㎡精装整租一室一厅", "一室一厅", "4800", "4500", "6.7空出", "民用水电"],
                    ["", "", "15-2-1501", "130㎡整租四室两卫带衣帽间燃气厨房", "四室两厅", "7200", "6900", "6.4空出", "民用水电"],
                    ["", "", "19-1102", "（整）65㎡精装整租一室一厅", "一室一厅", "4800", "4500", "685050#", "民用水电"],
                    ["", "香柠颜家府", "2-2-1401B", "两室一厅朝南带阳台", "两室一厅", "4500", "4200", "218619#", "水30/月，电1元/度"],
                ]
            )
            inventory._cache = inventory._normalize(frame)

            rows = await inventory.search("杨乐府现在还有什么房子", limit=8)

            self.assertEqual([row["房号"] for row in rows], ["1-704B", "9-1002", "15-2-1501", "19-1102"])
            self.assertTrue(all(row["小区"] == "杨乐府" for row in rows))
            self.assertEqual(rows[2]["押一付一"], "7200")
            self.assertEqual(rows[2]["押二付一"], "6900")
        finally:
            settings.inventory_source = previous_source

    async def test_spreadsheet_inventory_aliases_viewing_password_header(self) -> None:
        inventory = InventoryService()
        frame = inventory._spreadsheet_values_to_frame(
            [
                ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
                ["拱墅万达", "棠润府", "10-1004C", "一室一厅独立厨卫", "一室一厅", "2600", "2300", "101004#", "水30/月，电1元/度"],
            ]
        )
        frame = inventory._normalize(frame)

        self.assertIn("看房方式密码", frame.columns)
        self.assertEqual(frame.iloc[0]["看房方式密码"], "101004#")

    async def test_spreadsheet_inventory_preserves_standard_target_headers(self) -> None:
        inventory = InventoryService()
        frame = inventory._spreadsheet_values_to_frame(
            [
                ["区域", "小区", "房号", "户型描述", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
                ["拱墅万达", "棠润府", "10-1004C", "一室一厅独立厨卫", "一室一厅", "2600", "2300", "101004#", "水30/月，电1元/度"],
            ]
        )
        frame = inventory._normalize(frame)

        self.assertEqual(
            ["区域", "小区", "房号", "户型描述", "户型分类", "押一付一", "押二付一", "看房方式密码", "备注"],
            list(frame.columns),
        )

    async def test_discovers_latest_inventory_sheet_from_drive_folder(self) -> None:
        class FakeClient(FeishuClient):
            async def list_folder_files(self, folder_token: str) -> list[dict]:
                self.seen_folder_token = folder_token
                return [
                    {"name": "房源素材", "type": "folder", "token": "media"},
                    {"name": "旧房源表", "type": "sheet", "token": "old", "modified_time": "100"},
                    {"name": "寓你住一起房源表", "type": "sheet", "token": "new", "modified_time": "200"},
                ]

        client = FakeClient()
        inventory = InventoryService(client=client)
        token = await inventory._discover_inventory_sheet_token_from_drive_folder("root")

        self.assertEqual(token, "new")
        self.assertEqual(client.seen_folder_token, "root")

    async def test_exact_community_query_does_not_include_similar_communities(self) -> None:
        previous_source = settings.inventory_source
        try:
            settings.inventory_source = "local_cache"
            inventory = InventoryService()
            inventory._cache = pd.DataFrame(
                [
                    {"小区": "兴业杨家府", "房号": "10-1-304"},
                    {"小区": "兴业杨家府", "房号": "1-201"},
                    {"小区": "杨乐府", "房号": "1-704B"},
                    {"小区": "杨乐府", "房号": "9-1002"},
                    {"小区": "杨乐府", "房号": "15-2-1501"},
                    {"小区": "杨乐府", "房号": "19-1102"},
                ]
            )

            rows = await inventory.search("杨乐府现在还有什么房子", limit=8)

            self.assertEqual([row["房号"] for row in rows], ["1-704B", "9-1002", "15-2-1501", "19-1102"])
            self.assertTrue(all(row["小区"] == "杨乐府" for row in rows))
        finally:
            settings.inventory_source = previous_source

    async def test_strict_price_question_returns_empty_when_no_exact_price(self) -> None:
        previous_source = settings.inventory_source
        try:
            settings.inventory_source = "local_cache"
            inventory = InventoryService()
            inventory._cache = pd.DataFrame(
                [
                    {"小区": "杨乐府", "房号": "9-1002", "押一付": "4800", "押二付": "4500"},
                    {"小区": "杨乐府", "房号": "15-2-1501", "押一付": "7500", "押二付": "7200"},
                ]
            )

            rows = await inventory.search("杨乐府3500那套是几号房？", limit=8)

            self.assertEqual(rows, [])
        finally:
            settings.inventory_source = previous_source

    async def test_budget_question_keeps_nearby_price_candidates(self) -> None:
        previous_source = settings.inventory_source
        try:
            settings.inventory_source = "local_cache"
            inventory = InventoryService()
            inventory._cache = pd.DataFrame(
                [
                    {"小区": "兴业杨家府", "房号": "3-601", "押一付": "4600", "押二付": "4300"},
                    {"小区": "兴业杨家府", "房号": "10-1-304", "押一付": "4600", "押二付": "4300"},
                    {"小区": "兴业杨家府", "房号": "15-2-1501", "押一付": "7500", "押二付": "7200"},
                ]
            )

            rows = await inventory.search("兴业杨家府客户预算4200，推荐哪套？", limit=8)

            self.assertEqual([row["房号"] for row in rows], ["3-601", "10-1-304"])
        finally:
            settings.inventory_source = previous_source

    async def test_budget_question_returns_empty_when_area_has_only_expensive_rooms(self) -> None:
        previous_source = settings.inventory_source
        try:
            settings.inventory_source = "local_cache"
            inventory = InventoryService()
            inventory._cache = pd.DataFrame(
                [
                    {
                        "区域": "东新园 杭氧 新天地",
                        "小区": "东方茂",
                        "房号": "T3-1540",
                        "户型": "（整）70㎡精装整租一室一厅LOFT",
                        "户型分类": "一室一厅",
                        "押一付": "3800",
                        "押二付": "3500",
                    },
                    {
                        "区域": "东新园 杭氧 新天地",
                        "小区": "杨乐府",
                        "房号": "9-1002",
                        "户型": "（整）65㎡精装整租一室一厅",
                        "户型分类": "一室一厅",
                        "押一付": "4800",
                        "押二付": "4500",
                    },
                ]
            )

            rows = await inventory.search("新天地有没有2000左右的一室啊", limit=8)

            self.assertEqual(rows, [])
        finally:
            settings.inventory_source = previous_source

    async def test_area_budget_layout_search_finds_new_tiandi_4000_to_5000_two_room(self) -> None:
        previous_source = settings.inventory_source
        try:
            settings.inventory_source = "local_cache"
            inventory = InventoryService()
            inventory._cache = pd.DataFrame(
                [
                    {
                        "区域": "东新园 杭氧 新天地",
                        "小区": "新柠长木府",
                        "房号": "3-1002A",
                        "户型": "两室一厅朝南带阳台",
                        "户型分类": "两室一厅",
                        "押一付一": "4600",
                        "押二付一": "4300",
                    },
                    {
                        "区域": "东新园 杭氧 新天地",
                        "小区": "长浜龙吟轩",
                        "房号": "11-1603",
                        "户型": "两室一厅",
                        "户型分类": "两室一厅",
                        "押一付一": "4200",
                        "押二付一": "3900",
                    },
                    {
                        "区域": "东新园 杭氧 新天地",
                        "小区": "东方茂",
                        "房号": "T3-1540",
                        "户型": "一室一厅LOFT",
                        "户型分类": "一室一厅",
                        "押一付一": "3800",
                        "押二付一": "3500",
                    },
                ]
            )

            rows = await inventory.search("东新园 杭氧 新天地 4000-5000 两室 在租房源", limit=8)

            self.assertEqual([row["房号"] for row in rows], ["3-1002A", "11-1603"])
        finally:
            settings.inventory_source = previous_source

    async def test_negated_community_does_not_override_area_search(self) -> None:
        previous_source = settings.inventory_source
        try:
            settings.inventory_source = "local_cache"
            inventory = InventoryService()
            inventory._cache = pd.DataFrame(
                [
                    {
                        "区域": "石桥街道 华丰 石桥 永佳 半山",
                        "小区": "华丰欣苑",
                        "房号": "14-2-901",
                        "户型分类": "两室一厅",
                        "押一付一": "4900",
                    },
                    {
                        "区域": "石桥街道 华丰 石桥 永佳 半山",
                        "小区": "石桥铭苑",
                        "房号": "6-1102",
                        "户型分类": "两室一厅",
                        "押一付一": "4800",
                    },
                ]
            )

            rows = await inventory.search("石桥区域就行，不是只问石桥铭苑。", limit=8)

            self.assertEqual(
                [(row["小区"], row["房号"]) for row in rows],
                [("华丰欣苑", "14-2-901"), ("石桥铭苑", "6-1102")],
            )
        finally:
            settings.inventory_source = previous_source

    async def test_low_price_request_sorts_by_lowest_monthly_rent(self) -> None:
        previous_source = settings.inventory_source
        try:
            settings.inventory_source = "local_cache"
            inventory = InventoryService()
            inventory._cache = pd.DataFrame(
                [
                    {
                        "区域": "闸弄口 新塘 元宝塘 东站",
                        "小区": "骏塘名庭",
                        "房号": "6-1303",
                        "户型分类": "三室一厅",
                        "押一付": "7200",
                        "押二付": "6900",
                    },
                    {
                        "区域": "闸弄口 新塘 元宝塘 东站",
                        "小区": "骏塘名庭",
                        "房号": "8-1101A",
                        "户型分类": "一室",
                        "押一付": "1700",
                        "押二付": "1500",
                    },
                    {
                        "区域": "闸弄口 新塘 元宝塘 东站",
                        "小区": "京漾东韵府",
                        "房号": "4-2-601D",
                        "户型分类": "一室",
                        "押一付": "1800",
                        "押二付": "1500",
                    },
                ]
            )

            rows = await inventory.search("新塘附近有没有押一付一的低价房？", limit=8)

            self.assertEqual([row["房号"] for row in rows], ["8-1101A", "4-2-601D"])
        finally:
            settings.inventory_source = previous_source

    async def test_cheap_request_does_not_prioritize_expensive_video_candidates(self) -> None:
        previous_source = settings.inventory_source
        try:
            settings.inventory_source = "local_cache"
            inventory = InventoryService()
            inventory._cache = pd.DataFrame(
                [
                    {
                        "区域": "闸弄口 新塘 元宝塘 东站",
                        "小区": "骏塘名庭",
                        "房号": "6-1303",
                        "户型分类": "三室一厅",
                        "押一付": "7200",
                        "押二付": "6900",
                    },
                    {
                        "区域": "闸弄口 新塘 元宝塘 东站",
                        "小区": "骏塘名庭",
                        "房号": "8-1101A",
                        "户型分类": "一室",
                        "押一付": "1700",
                        "押二付": "1500",
                    },
                    {
                        "区域": "闸弄口 新塘 元宝塘 东站",
                        "小区": "京漾东韵府",
                        "房号": "4-2-601D",
                        "户型分类": "一室",
                        "押一付": "1800",
                        "押二付": "1500",
                    },
                ]
            )

            rows = await inventory.search("元宝塘附近客户想看便宜点的，有视频的优先。", limit=8)

            self.assertEqual([row["房号"] for row in rows], ["8-1101A", "4-2-601D"])
        finally:
            settings.inventory_source = previous_source

    async def test_room_type_request_is_hard_filter_for_area_budget_search(self) -> None:
        previous_source = settings.inventory_source
        try:
            settings.inventory_source = "local_cache"
            inventory = InventoryService()
            inventory._cache = pd.DataFrame(
                [
                    {
                        "区域": "东站 皋塘 彭埠",
                        "小区": "京漾东韵府",
                        "房号": "4-2-601B",
                        "户型": "一室朝南独立厨卫",
                        "户型分类": "一室",
                        "押一付": "2000",
                    },
                    {
                        "区域": "东站 皋塘 彭埠",
                        "小区": "皋塘运都",
                        "房号": "9-2-402B",
                        "户型": "一室一厅朝南带阳台",
                        "户型分类": "一室一厅",
                        "押一付": "2600",
                    },
                    {
                        "区域": "东站 皋塘 彭埠",
                        "小区": "东站两室小区",
                        "房号": "1-201",
                        "户型": "两室一厅",
                        "户型分类": "两室一厅",
                        "押一付": "4200",
                    },
                ]
            )

            rows = await inventory.search("东站有没有2000左右的两室啊", limit=8)

            self.assertEqual(rows, [])
        finally:
            settings.inventory_source = previous_source

    async def test_prefers_last_explicit_community_when_similar_communities_are_mentioned(self) -> None:
        previous_source = settings.inventory_source
        try:
            settings.inventory_source = "local_cache"
            inventory = InventoryService()
            inventory._cache = pd.DataFrame(
                [
                    {"小区": "兴业杨家府", "房号": "10-1-304"},
                    {"小区": "兴业杨家府", "房号": "1-201"},
                    {"小区": "杨乐府", "房号": "1-704B"},
                    {"小区": "杨乐府", "房号": "9-1002"},
                    {"小区": "杨乐府", "房号": "19-1102"},
                ]
            )

            rows = await inventory.search("杨乐府和兴业杨家府别搞混，杨乐府还有几套？", limit=8)

            self.assertEqual([row["房号"] for row in rows], ["1-704B", "9-1002", "19-1102"])
            self.assertTrue(all(row["小区"] == "杨乐府" for row in rows))
        finally:
            settings.inventory_source = previous_source

    async def test_search_supports_multi_segment_room_numbers(self) -> None:
        previous_source = settings.inventory_source
        try:
            settings.inventory_source = "local_cache"
            inventory = InventoryService()
            inventory._cache = pd.DataFrame(
                [
                    {"小区": "金昌苑", "房号": "2-2-1601-E"},
                    {"小区": "孔家埭和府", "房号": "1-1-901-1"},
                ]
            )

            rows = await inventory.search("金昌苑2-2-1601-E视频发一下", limit=3)

            self.assertEqual([(row["小区"], row["房号"]) for row in rows], [("金昌苑", "2-2-1601-E")])
        finally:
            settings.inventory_source = previous_source

    async def test_fuzzy_community_requires_two_common_chinese_chars(self) -> None:
        previous_source = settings.inventory_source
        try:
            settings.inventory_source = "local_cache"
            inventory = InventoryService()
            inventory._cache = pd.DataFrame(
                [
                    {"小区": "永佳新苑", "房号": "2-703"},
                    {"小区": "星桥桂花城", "房号": "1-101"},
                ]
            )

            rows = await inventory.search("永住新苑还有房吗", limit=3)
            weak_rows = await inventory.search("永桥附近有房吗", limit=3)

            self.assertEqual([row["小区"] for row in rows], ["永佳新苑"])
            self.assertEqual(weak_rows, [])
        finally:
            settings.inventory_source = previous_source

    async def test_normalizes_broker_typo_and_building_room_queries(self) -> None:
        previous_source = settings.inventory_source
        try:
            settings.inventory_source = "local_cache"
            inventory = InventoryService()
            inventory._cache = pd.DataFrame(
                [
                    {"小区": "棠润府", "房号": "7-803A", "户型": "一室一厅带阳台"},
                    {"小区": "臻棠樾府", "房号": "7-2-1101", "户型": "两室一厅"},
                    {"小区": "杨乐府", "房号": "9-1002", "户型": "一室一厅"},
                    {"小区": "兴业杨家府", "房号": "10-1-304", "户型": "一室一厅"},
                    {"小区": "小洋坝", "房号": "1-101", "户型": "一室一厅"},
                    {"小区": "华丰人家", "房号": "8-603", "户型": "一室一厅"},
                    {"小区": "华丰欣苑", "房号": "14-2-901", "户型": "一室一厅"},
                ]
            )

            typo_rows = await inventory.search("棠润肤7-803A还能看吗", limit=3)
            tangrun_rows = await inventory.search("棠闰府还有房吗", limit=5)
            yangle_rows = await inventory.search("杨了府还有房吗", limit=5)
            homophone_rows = await inventory.search("小洋吧还有房吗", limit=3)
            too_many_typos_rows = await inventory.search("堂润肤还有房吗", limit=3)
            building_rows = await inventory.search("华丰8幢603视频有吗", limit=3)

            self.assertEqual([(row["小区"], row["房号"]) for row in typo_rows], [("棠润府", "7-803A")])
            self.assertEqual([row["小区"] for row in tangrun_rows], ["棠润府"])
            self.assertEqual([row["小区"] for row in yangle_rows], ["杨乐府"])
            self.assertEqual([(row["小区"], row["房号"]) for row in homophone_rows], [("小洋坝", "1-101")])
            self.assertEqual(too_many_typos_rows, [])
            self.assertEqual([(row["小区"], row["房号"]) for row in building_rows], [("华丰人家", "8-603")])
        finally:
            settings.inventory_source = previous_source

    async def test_explicit_room_number_does_not_fuzzy_match_different_room(self) -> None:
        previous_source = settings.inventory_source
        try:
            settings.inventory_source = "local_cache"
            inventory = InventoryService()
            inventory._cache = pd.DataFrame(
                [
                    {"小区": "杨乐府", "房号": "15-2-1501"},
                    {"小区": "范珺悦邸", "房号": "4-1201B"},
                ]
            )

            rows = await inventory.search("2-501视频发我一下", limit=3)

            self.assertEqual(rows, [])
        finally:
            settings.inventory_source = previous_source

    async def test_typoed_community_only_query_returns_all_rooms_in_that_community(self) -> None:
        previous_source = settings.inventory_source
        try:
            settings.inventory_source = "local_cache"
            inventory = InventoryService()
            inventory._cache = pd.DataFrame(
                [
                    {"小区": "棠润府", "房号": "1-602A"},
                    {"小区": "棠润府", "房号": "7-803A"},
                    {"小区": "棠润府", "房号": "10-1004C"},
                    {"小区": "小洋坝", "房号": "1-101"},
                ]
            )

            rows = await inventory.search("棠润肤还有房吗", limit=8)
            too_many_typo_rows = await inventory.search("堂润肤还有房吗", limit=8)

            self.assertEqual(
                [(row["小区"], row["房号"]) for row in rows],
                [("棠润府", "1-602A"), ("棠润府", "7-803A"), ("棠润府", "10-1004C")],
            )
            self.assertEqual(too_many_typo_rows, [])
        finally:
            settings.inventory_source = previous_source

    async def test_fuzzy_community_allows_only_one_typo(self) -> None:
        previous_source = settings.inventory_source
        try:
            settings.inventory_source = "local_cache"
            inventory = InventoryService()
            inventory._cache = pd.DataFrame(
                [
                    {"小区": "合嵣悦府", "房号": "8-2101B", "户型": "一室一厅带燃气阳台"},
                    {"小区": "合嵣悦府", "房号": "12-1804A", "户型": "一室朝南内厨内卫"},
                    {"小区": "棠润府", "房号": "1-602A"},
                ]
            )

            one_typo_rows = await inventory.search("合塘悦府那套房子有笔记吗", limit=8)
            two_typo_rows = await inventory.search("合塘乐府那套房子有笔记吗", limit=8)
            extra_typo_rows = await inventory.search("合塘悦府府那套房子有笔记吗", limit=8)

            self.assertEqual(
                [(row["小区"], row["房号"]) for row in one_typo_rows],
                [("合嵣悦府", "8-2101B"), ("合嵣悦府", "12-1804A")],
            )
            self.assertEqual(two_typo_rows, [])
            self.assertEqual(extra_typo_rows, [])
        finally:
            settings.inventory_source = previous_source
