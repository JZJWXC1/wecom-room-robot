import unittest

import pandas as pd

from app.config import settings
from app.services.inventory import InventoryService


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
