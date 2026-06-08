import unittest
import tempfile
from pathlib import Path

import pandas as pd

from app.config import settings
from app.services.inventory import InventoryService
import app.services.inventory as inventory_module


class InventoryFuzzyMatchingTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_feishu_inventory_refreshes_after_ttl(self) -> None:
        previous_source = settings.inventory_source
        previous_refresh_seconds = settings.inventory_refresh_seconds
        previous_cache_path = settings.inventory_cache_path
        previous_sheet_token = settings.feishu_inventory_sheet_token
        previous_client = inventory_module.FeishuClient
        calls = 0

        class FakeFeishuClient:
            async def read_bitable_dataframe(self) -> pd.DataFrame:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return pd.DataFrame(
                        [{"小区": "星桥锦绣嘉苑", "房号": "21-1801B"}]
                    )
                return pd.DataFrame([{"小区": "棠润府", "房号": "15-2-1901B"}])

        try:
            with tempfile.TemporaryDirectory() as directory:
                settings.inventory_source = "feishu_bitable"
                settings.feishu_inventory_sheet_token = ""
                settings.inventory_refresh_seconds = 300
                settings.inventory_cache_path = Path(directory) / "inventory_cache.csv"
                inventory_module.FeishuClient = FakeFeishuClient
                inventory = InventoryService()

                first_rows = await inventory.search("星桥房子还在吗")
                second_rows = await inventory.search("星桥房子还在吗")
                settings.inventory_refresh_seconds = 0
                third_rows = await inventory.search("星桥房子还在吗")

                self.assertEqual(len(first_rows), 1)
                self.assertEqual(len(second_rows), 1)
                self.assertEqual(third_rows, [])
                self.assertEqual(calls, 2)
        finally:
            settings.inventory_source = previous_source
            settings.inventory_refresh_seconds = previous_refresh_seconds
            settings.inventory_cache_path = previous_cache_path
            settings.feishu_inventory_sheet_token = previous_sheet_token
            inventory_module.FeishuClient = previous_client

    async def test_feishu_inventory_prefers_spreadsheet_when_configured(self) -> None:
        previous_source = settings.inventory_source
        previous_cache_path = settings.inventory_cache_path
        previous_sheet_token = settings.feishu_inventory_sheet_token
        previous_client = inventory_module.FeishuClient

        class FakeFeishuClient:
            async def read_spreadsheet_values(self) -> dict:
                return {
                    "values": [
                        ["营销标题"],
                        ["区域", "小区", "房号", "户型", "户型分类", "押一付一", "押二付一", "密码", "备注"],
                        ["拱墅", "棠润府", "1-602A", "一室一厅带阳台", "一室一厅", "1700", "1500", "6.15空出", "水30/月"],
                        ["", "", "10-1004C", "一室一厅独立厨卫", "一室一厅", "2800", "2600", "101004#", "水30/月"],
                    ]
                }

            async def read_bitable_dataframe(self) -> pd.DataFrame:
                return pd.DataFrame([{"小区": "星桥锦绣嘉苑", "房号": "21-1801-2"}])

        try:
            with tempfile.TemporaryDirectory() as directory:
                settings.inventory_source = "feishu_bitable"
                settings.feishu_inventory_sheet_token = "sheet_token"
                settings.inventory_cache_path = Path(directory) / "inventory_cache.csv"
                inventory_module.FeishuClient = FakeFeishuClient
                inventory = InventoryService()

                star_rows = await inventory.search("星桥还有房子吗")
                tang_rows = await inventory.search("棠润府还有吗", limit=3)

                self.assertEqual(star_rows, [])
                self.assertEqual(len(tang_rows), 2)
                self.assertEqual(tang_rows[0]["小区"], "棠润府")
                self.assertEqual(tang_rows[1]["小区"], "棠润府")
                self.assertEqual(tang_rows[1]["房号"], "10-1004C")
        finally:
            settings.inventory_source = previous_source
            settings.inventory_cache_path = previous_cache_path
            settings.feishu_inventory_sheet_token = previous_sheet_token
            inventory_module.FeishuClient = previous_client
